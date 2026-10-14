from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
import json
import time
import random

from .client import RateLimitReached, TokenStore, XeroApiError, XeroClient
from .config import Settings
from .exporters import write_csv, write_csv_rows, write_manifest, write_page_json
from .models import ENDPOINTS, EndpointSpec
from .state import StateStore
from .utils import ensure_dir, flatten_record, slugify


class BackupRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        modified_since: str | None = None,
        delta_id: str | None = None,
    ) -> None:
        """`delta_id` fetches only records changed since `modified_since` and
        merges them into the existing backup in place (latest version wins)."""
        if delta_id and not modified_since:
            raise ValueError("A delta run needs modified_since.")
        self.settings = settings
        self.modified_since = modified_since
        self.delta_id = delta_id
        self.state = StateStore(settings.state_db)
        self.client = XeroClient(settings, TokenStore(settings.token_file))
        self.connection = self._select_connection()
        self.tenant_id = self.connection["tenantId"]
        self.tenant_slug = slugify(self.connection.get("tenantName", self.tenant_id))
        self.output_root = ensure_dir(settings.output_dir / self.tenant_slug)
        self.records_cache: dict[str, list[dict[str, Any]]] = {}

    def _select_connection(self) -> dict[str, Any]:
        connections = self.client.list_connections()
        active = [row for row in connections if row.get("tenantType") == "ORGANISATION"]
        if not active:
            raise RuntimeError("No active Xero organisation connections were found.")
        if self.settings.tenant_id:
            for row in active:
                if row.get("tenantId") == self.settings.tenant_id:
                    return row
            raise RuntimeError("Configured XERO_TENANT_ID was not found in active connections.")
        return active[0]

    def run(self, *, sleep_on_rate_limit: bool = False) -> None:
        manifest: dict[str, Any] | None = None
        sleep_after: int | None = None
        try:
            self.phase_one_fetch_data()
            self.phase_two_discover_attachments()
            self.phase_three_download_attachments()
            # Attachments found and downloaded after the records phase must
            # reach attachments_manifest.csv, which conversion reads.
            self.write_attachment_exports()
            manifest = self.build_manifest(status="complete")
        except RateLimitReached as exc:
            self.state.commit()
            manifest = self.build_manifest(status="paused", rate_limit_reason=exc.reason)
            if sleep_on_rate_limit and exc.retry_after_seconds:
                sleep_after = exc.retry_after_seconds
            else:
                print(exc.reason)
                if exc.retry_after_seconds:
                    print(f"Retry after approximately {exc.retry_after_seconds} seconds.")
        finally:
            self.state.commit()
            if manifest is not None:
                write_manifest(self.output_root, manifest)
            self.state.close()

        if sleep_after:
            print(f"Rate limit reached. Sleeping for {sleep_after} seconds.")
            time.sleep(sleep_after)
            BackupRunner(
                self.settings, modified_since=self.modified_since, delta_id=self.delta_id
            ).run(sleep_on_rate_limit=False)
            return
        if manifest and manifest["status"] == "complete":
            print(f"Backup complete. Output written to {self.output_root}")

    def _status_key(self, key: str) -> str:
        return f"{key}@{self.delta_id}" if self.delta_id else key

    def phase_one_fetch_data(self) -> None:
        for endpoint in ENDPOINTS:
            status = self.state.get_sync_status(self._status_key(endpoint.key))
            if status and status["completed"]:
                continue
            self._sync_endpoint(endpoint)

        for endpoint_key, records in self.records_cache.items():
            write_csv(self.output_root, endpoint_key, records)
        self.write_attachment_exports()

    def _sync_endpoint(self, endpoint: EndpointSpec) -> None:
        status_key = self._status_key(endpoint.key)
        status = self.state.get_sync_status(status_key)
        page = int(status["last_page"]) if status else 1
        last_cursor = status["last_cursor"] if status else None
        while True:
            params = self._build_params(endpoint, page, last_cursor)
            try:
                extra_headers = (
                    {"If-Modified-Since": self.modified_since} if self.modified_since else None
                )
                payload, headers = self.client.api_get_json(
                    endpoint.path, self.tenant_id, params=params, extra_headers=extra_headers
                )
            except XeroApiError as exc:
                if endpoint.optional and exc.status_code in {401, 403, 404}:
                    self.state.upsert_sync_status(status_key, completed=True, last_error=str(exc))
                    print(f"Skipping optional endpoint {endpoint.key}: {exc}")
                    return
                self.state.upsert_sync_status(status_key, last_page=page, last_cursor=last_cursor, last_error=str(exc))
                raise
            records = payload.get(endpoint.response_key, [])
            cursor_label = self._cursor_label(endpoint, page, last_cursor)
            write_page_json(self.output_root, endpoint.key, cursor_label, records)
            self.records_cache[endpoint.key] = self._load_existing_records(endpoint.key, refresh=True)
            write_csv(self.output_root, endpoint.key, self.records_cache[endpoint.key])
            self._track_entities(endpoint, records)
            self.state.commit()
            self._raise_if_header_limits_exhausted(headers)

            if endpoint.pagination_mode == "none":
                self.state.upsert_sync_status(status_key, last_page=1, completed=True)
                return
            if not records:
                self.state.upsert_sync_status(status_key, last_page=page, last_cursor=last_cursor, completed=True)
                return
            if endpoint.pagination_mode == "page":
                if len(records) < endpoint.page_size:
                    self.state.upsert_sync_status(status_key, last_page=page, completed=True)
                    return
                page += 1
                self.state.upsert_sync_status(status_key, last_page=page)
                continue

            next_cursor = records[-1].get("JournalNumber")
            if next_cursor in {None, last_cursor}:
                self.state.upsert_sync_status(status_key, last_page=page, last_cursor=str(next_cursor), completed=True)
                return
            last_cursor = str(next_cursor)
            page += 1
            self.state.upsert_sync_status(status_key, last_page=page, last_cursor=last_cursor)

    def _load_existing_records(self, endpoint_key: str, refresh: bool = False) -> list[dict[str, Any]]:
        if endpoint_key in self.records_cache and not refresh:
            return self.records_cache[endpoint_key]
        raw_dir = self.output_root / "raw" / endpoint_key
        spec = next((e for e in ENDPOINTS if e.key == endpoint_key), None)
        id_field = spec.entity_id_field if spec else None
        merged: dict[str, dict[str, Any]] = {}
        anonymous: list[dict[str, Any]] = []
        if raw_dir.exists():
            # Full-backup pages first, then delta pages oldest to newest, so
            # the latest version of each record wins.
            paths = sorted(raw_dir.glob("*.json"), key=lambda p: (p.name.startswith("delta-"), p.name))
            for path in paths:
                for record in json.loads(path.read_text(encoding="utf-8")):
                    record_id = record.get(id_field) if id_field else None
                    if record_id:
                        merged[record_id] = record
                    else:
                        anonymous.append(record)
        records = [*merged.values(), *anonymous]
        self.records_cache[endpoint_key] = records
        return records

    def _build_params(self, endpoint: EndpointSpec, page: int, last_cursor: str | None) -> dict[str, Any] | None:
        if endpoint.pagination_mode == "page":
            return {"page": page}
        if endpoint.pagination_mode == "offset":
            return {"offset": last_cursor} if last_cursor else None
        return None

    def _cursor_label(self, endpoint: EndpointSpec, page: int, last_cursor: str | None) -> str:
        prefix = f"delta-{self.delta_id}-" if self.delta_id else ""
        if endpoint.pagination_mode == "page":
            return f"{prefix}page-{page:05d}"
        if endpoint.pagination_mode == "offset":
            suffix = last_cursor or "start"
            return f"{prefix}offset-{suffix}"
        return f"{prefix}full"

    def _track_entities(self, endpoint: EndpointSpec, records: list[dict[str, Any]]) -> None:
        if not endpoint.entity_id_field:
            return
        for record in records:
            entity_id = record.get(endpoint.entity_id_field)
            if not entity_id:
                continue
            has_attachments = bool(record.get("HasAttachments", False))
            self.state.upsert_entity(entity_id, endpoint.key, has_attachments)
            if self.delta_id and has_attachments:
                # A changed record may carry new attachments: look again.
                self.state.mark_entity_undiscovered(entity_id, endpoint.key)
        self.state.commit()

    def phase_two_discover_attachments(self) -> None:
        phase_key = self._status_key("attachment_discovery")
        status = self.state.get_sync_status(phase_key)
        if status and status["completed"]:
            return
        for entity in self.state.iter_entities_pending_attachment_discovery():
            endpoint = self._attachment_endpoint_for_type(entity["type"])
            if not endpoint:
                self.state.mark_entity_discovered(entity["entity_id"], entity["type"])
                self.state.commit()
                continue
            path = f"/{endpoint.attachment_entity_type}/{entity['entity_id']}/Attachments"
            try:
                payload, headers = self.client.api_get_json(path, self.tenant_id)
            except XeroApiError as exc:
                if exc.status_code in {401, 403, 404} and endpoint.optional:
                    self.state.mark_entity_discovered(entity["entity_id"], entity["type"])
                    self.state.commit()
                    continue
                self.state.upsert_sync_status(phase_key, last_cursor=f"{entity['type']}:{entity['entity_id']}", last_error=str(exc))
                raise
            attachments = payload.get("Attachments", [])
            for item in attachments:
                self.state.enqueue_attachment(
                    entity_id=entity["entity_id"],
                    entity_type=entity["type"],
                    file_name=item.get("FileName") or item.get("FileName", ""),
                    attachment_id=item.get("AttachmentID"),
                    mime_type=item.get("MimeType"),
                )
            self.state.mark_entity_discovered(entity["entity_id"], entity["type"])
            self.state.commit()
            self._raise_if_header_limits_exhausted(headers)
        self.state.upsert_sync_status(phase_key, completed=True)

    def phase_three_download_attachments(self) -> None:
        phase_key = self._status_key("attachment_downloads")
        status = self.state.get_sync_status(phase_key)
        if status and status["completed"]:
            return
        for attachment in self.state.iter_pending_attachments():
            endpoint = self._attachment_endpoint_for_type(attachment["type"])
            if not endpoint:
                continue
            encoded_name = quote(attachment["file_name"])
            url = f"{self.settings.api_base_url}/{endpoint.attachment_entity_type}/{attachment['entity_id']}/Attachments/{encoded_name}"
            body, headers = self.client.api_get_bytes(
                url,
                self.tenant_id,
                extra_headers={"Accept": "application/octet-stream"},
            )
            attachment_dir = ensure_dir(self.output_root / "attachments" / attachment["type"] / attachment["entity_id"])
            target = attachment_dir / attachment["file_name"]
            target.write_bytes(body)
            self.state.mark_attachment_downloaded(
                attachment["entity_id"],
                attachment["type"],
                attachment["file_name"],
                str(target),
            )
            self.state.commit()
            self._raise_if_header_limits_exhausted(headers)
        self.state.upsert_sync_status(phase_key, completed=True)

    def _attachment_endpoint_for_type(self, entity_type: str) -> EndpointSpec | None:
        for endpoint in ENDPOINTS:
            if endpoint.key == entity_type and endpoint.supports_attachments:
                return endpoint
        return None

    def build_manifest(self, *, status: str, rate_limit_reason: str | None = None) -> dict[str, Any]:
        summary = self.state.get_summary()
        sync_status_rows = self.state.connection.execute(
            "SELECT endpoint, last_page, last_cursor, completed, updated_at, last_error FROM sync_status ORDER BY endpoint"
        ).fetchall()
        return {
            "status": status,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tenant": {
                "tenant_id": self.tenant_id,
                "tenant_name": self.connection.get("tenantName"),
            },
            "summary": summary,
            "last_rate_limits": self.client.last_response_limits,
            "rate_limit_reason": rate_limit_reason,
            "sync_status": [dict(row) for row in sync_status_rows],
        }

    def _raise_if_header_limits_exhausted(self, headers: dict[str, str]) -> None:
        day_remaining = self.client.last_response_limits["day_remaining"]
        minute_remaining = self.client.last_response_limits["minute_remaining"]
        if day_remaining is not None and day_remaining <= self.settings.daily_call_floor:
            raise RateLimitReached(
                f"Stopping early because only {day_remaining} daily Xero calls remain.",
                day_remaining=day_remaining,
                minute_remaining=minute_remaining,
            )
        if day_remaining == 0:
            raise RateLimitReached(
                "Daily Xero call limit reached",
                day_remaining=day_remaining,
                minute_remaining=minute_remaining,
            )
        if minute_remaining == 0:
            raise RateLimitReached(
                "Per-minute Xero call limit reached",
                day_remaining=day_remaining,
                minute_remaining=minute_remaining,
            )

    def verify_attachments(self, *, sample_size: int | None = None, full: bool = False) -> dict[str, Any]:
        entities = self.state.list_entities_with_attachments()
        if not entities:
            return {"checked_entities": 0, "mismatches": 0, "details": []}

        if full:
            selected = entities
        else:
            size = min(sample_size or 25, len(entities))
            selected = random.sample(entities, size)

        details: list[dict[str, Any]] = []
        mismatches = 0
        for entity in selected:
            endpoint = self._attachment_endpoint_for_type(entity["type"])
            if not endpoint:
                continue
            path = f"/{endpoint.attachment_entity_type}/{entity['entity_id']}/Attachments"
            payload, _headers = self.client.api_get_json(path, self.tenant_id)
            remote_files = {item.get("FileName") for item in payload.get("Attachments", []) if item.get("FileName")}

            local_rows = self.state.list_attachments_for_entity(entity["entity_id"], entity["type"])
            local_files = {row["file_name"] for row in local_rows}
            missing_local = sorted(remote_files - local_files)
            extra_local = sorted(local_files - remote_files)
            missing_files_on_disk = []
            for row in local_rows:
                local_path = row["local_path"]
                if local_path and not Path(local_path).exists():
                    missing_files_on_disk.append(local_path)

            if missing_local or extra_local or missing_files_on_disk:
                mismatches += 1
                details.append(
                    {
                        "entity_type": entity["type"],
                        "entity_id": entity["entity_id"],
                        "missing_local": missing_local,
                        "extra_local": extra_local,
                        "missing_files_on_disk": missing_files_on_disk,
                    }
                )

        return {
            "checked_entities": len(selected),
            "mismatches": mismatches,
            "details": details,
            "mode": "full" if full else "sample",
        }

    def write_attachment_exports(self) -> None:
        attachment_rows = self.state.list_all_attachments()
        if not attachment_rows:
            return

        indexed_records = {
            "invoices": self._index_records("invoices", "InvoiceID"),
            "bank_transactions": self._index_records("bank_transactions", "BankTransactionID"),
            "receipts": self._index_records("receipts", "ReceiptID"),
        }
        manifest_rows: list[dict[str, str]] = []
        attachment_index: dict[tuple[str, str], list[dict[str, str]]] = {}

        for row in attachment_rows:
            entity_type = row["type"]
            entity_id = row["entity_id"]
            record = indexed_records.get(entity_type, {}).get(entity_id, {})
            relative_path = row["local_path"] or str(
                Path("attachments") / entity_type / entity_id / row["file_name"]
            )
            manifest_row = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "attachment_id": row["attachment_id"] or "",
                "file_name": row["file_name"],
                "mime_type": row["mime_type"] or "",
                "downloaded": str(row["downloaded"]),
                "local_path": relative_path,
                **self._record_summary(entity_type, record),
            }
            manifest_rows.append(manifest_row)
            attachment_index.setdefault((entity_type, entity_id), []).append(manifest_row)

        write_csv_rows(self.output_root, "attachments_manifest.csv", manifest_rows)
        self._rewrite_endpoint_csv_with_attachment_columns("invoices", "InvoiceID", attachment_index)
        self._rewrite_endpoint_csv_with_attachment_columns("bank_transactions", "BankTransactionID", attachment_index)
        self._rewrite_endpoint_csv_with_attachment_columns("receipts", "ReceiptID", attachment_index)

    def _index_records(self, endpoint_key: str, id_field: str) -> dict[str, dict[str, Any]]:
        records = self._load_existing_records(endpoint_key)
        indexed: dict[str, dict[str, Any]] = {}
        for record in records:
            record_id = record.get(id_field)
            if record_id:
                indexed[str(record_id)] = record
        return indexed

    def _record_summary(self, entity_type: str, record: dict[str, Any]) -> dict[str, str]:
        if entity_type == "invoices":
            contact = record.get("Contact") or {}
            return {
                "document_number": str(record.get("InvoiceNumber", "")),
                "reference": str(record.get("Reference", "")),
                "contact_name": str(contact.get("Name", "")),
                "date": str(record.get("DateString") or record.get("Date") or ""),
                "status": str(record.get("Status", "")),
            }
        if entity_type == "bank_transactions":
            contact = record.get("Contact") or {}
            return {
                "document_number": str(record.get("BankTransactionID", "")),
                "reference": str(record.get("Reference", "")),
                "contact_name": str(contact.get("Name", "")),
                "date": str(record.get("DateString") or record.get("Date") or ""),
                "status": str(record.get("Status", "")),
            }
        if entity_type == "receipts":
            user = record.get("User") or {}
            return {
                "document_number": str(record.get("ReceiptID", "")),
                "reference": str(record.get("Reference", "")),
                "contact_name": str(user.get("Name", "")),
                "date": str(record.get("DateString") or record.get("Date") or ""),
                "status": str(record.get("Status", "")),
            }
        return {
            "document_number": "",
            "reference": "",
            "contact_name": "",
            "date": "",
            "status": "",
        }

    def _rewrite_endpoint_csv_with_attachment_columns(
        self,
        endpoint_key: str,
        id_field: str,
        attachment_index: dict[tuple[str, str], list[dict[str, str]]],
    ) -> None:
        records = self._load_existing_records(endpoint_key)
        if not records:
            return
        rows: list[dict[str, str]] = []
        for record in records:
            row = dict(flatten_record(record))
            record_id = str(record.get(id_field, ""))
            attachments = attachment_index.get((endpoint_key, record_id), [])
            row["AttachmentCount"] = str(len(attachments))
            row["AttachmentFileNames"] = "|".join(item["file_name"] for item in attachments)
            row["AttachmentPaths"] = "|".join(item["local_path"] for item in attachments)
            rows.append(row)
        write_csv_rows(self.output_root, f"{endpoint_key}.csv", rows)


def reset_phase(settings: Settings, phase: str) -> None:
    state = StateStore(settings.state_db)
    try:
        if phase == "attachments":
            state.clear_endpoint("attachment_discovery")
            state.clear_endpoint("attachment_downloads")
            state.connection.execute("UPDATE entities SET attachment_discovered = 0 WHERE has_attachments = 1")
            state.connection.execute("UPDATE attachments_queue SET downloaded = 0, local_path = NULL")
        else:
            state.clear_endpoint(phase)
        state.commit()
    finally:
        state.close()
