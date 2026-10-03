export type Role = "admin" | "reviewer" | "preparer";

export type User = {
  id: number;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
};

export type Company = {
  id: number;
  name: string;
  external_company_name: string;
  gstin: string | null;
  state: string | null;
  connector_type: string;
  connector_url: string | null;
  auto_create_ledgers: boolean;
  always_review: boolean;
  /** Decimal as a string, e.g. "100000.00"; null when there is no limit. */
  review_above_amount: string | null;
  auto_post: boolean;
  created_at: string;
};

export type CompanyInput = {
  name: string;
  external_company_name: string;
  gstin?: string | null;
  state?: string | null;
};

export type Ledger = {
  id: number;
  name: string;
  parent: string | null;
  gstin: string | null;
  state: string | null;
  aliases: string[];
  synced_at: string;
};

export type LedgerGroup = { name: string; parent: string | null };

export type TallyStatus = { ok: boolean; detail: string; url: string };

export type TallyCompany = { name: string; state: string | null; gstin: string | null };

export type SyncResult = { ledgers: number; groups: number; added: number; removed: number };

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

function errorMessage(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail[0]?.msg) {
      const first = detail[0] as { loc?: string[]; msg: string };
      const field = first.loc?.at(-1);
      return field ? `${field}: ${first.msg}` : first.msg;
    }
  }
  return `Request failed (${status})`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    credentials: "same-origin",
  });
  if (resp.status === 204) return undefined as T;
  const body = await resp.json().catch(() => null);
  if (!resp.ok) throw new ApiError(resp.status, errorMessage(body, resp.status));
  return body as T;
}

const post = <T>(path: string, data?: unknown) =>
  request<T>(path, { method: "POST", body: data === undefined ? undefined : JSON.stringify(data) });

const patch = <T>(path: string, data: unknown) =>
  request<T>(path, { method: "PATCH", body: JSON.stringify(data) });

export const api = {
  setupStatus: () => request<{ needs_setup: boolean }>("/api/auth/setup"),
  setup: (data: { email: string; full_name: string; password: string }) =>
    post<User>("/api/auth/setup", data),
  login: (data: { email: string; password: string }) => post<User>("/api/auth/login", data),
  logout: () => post<void>("/api/auth/logout"),
  me: () => request<User>("/api/auth/me"),

  companies: () => request<Company[]>("/api/companies"),
  company: (id: number) => request<Company>(`/api/companies/${id}`),
  createCompany: (data: CompanyInput) => post<Company>("/api/companies", data),
  updateCompany: (id: number, data: Partial<Company>) =>
    patch<Company>(`/api/companies/${id}`, data),

  ledgers: (id: number, q?: string, parent?: string) => {
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (parent) params.set("parent", parent);
    const qs = params.size ? `?${params}` : "";
    return request<Ledger[]>(`/api/companies/${id}/ledgers${qs}`);
  },
  groups: (id: number) => request<LedgerGroup[]>(`/api/companies/${id}/groups`),
  syncLedgers: (id: number) => post<SyncResult>(`/api/companies/${id}/ledgers/sync`),

  tallyStatus: () => request<TallyStatus>("/api/connectors/tally/status"),
  tallyCompanies: () => request<TallyCompany[]>("/api/connectors/tally/companies"),
};

// -- documents ----------------------------------------------------------------------

export type DocumentStatus = "uploaded" | "parsing" | "parsed" | "failed" | "duplicate";

export type DocumentSummary = {
  id: number;
  company_id: number;
  original_filename: string;
  mime_type: string;
  kind: "pdf" | "image" | "docx" | "sheet";
  size_bytes: number;
  status: DocumentStatus;
  error: string | null;
  duplicate_of_id: number | null;
  page_count: number;
  has_text_layer: boolean;
  uploaded_by: number | null;
  uploader_name: string | null;
  created_at: string;
  parsed_at: string | null;
  voucher_id: number | null;
  voucher_status: VoucherStatus | null;
  party_name: string | null;
  invoice_number: string | null;
  grand_total: number | null;
};

export type ParsedPage = { number: number; width: number; height: number; has_text: boolean };
export type ParsedSheet = { name: string; rows: string[][]; total_rows: number };

export type DocumentDetail = DocumentSummary & {
  sha256: string;
  text: string | null;
  parsed: { pages?: ParsedPage[]; sheets?: ParsedSheet[]; warnings?: string[] };
};

export type UploadResult = {
  documents: DocumentSummary[];
  rejected: { filename: string; reason: string }[];
};

export const MAX_UPLOAD_MB = 25; // keep in step with MAX_UPLOAD_MB on the backend
const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;

export const documentsApi = {
  list: (companyId: number, status?: DocumentStatus[]) => {
    const params = new URLSearchParams();
    status?.forEach((s) => params.append("status", s));
    const qs = params.size ? `?${params}` : "";
    return request<DocumentSummary[]>(`/api/companies/${companyId}/documents${qs}`);
  },
  counts: (companyId: number) =>
    request<Record<DocumentStatus, number>>(`/api/companies/${companyId}/documents/counts`),
  get: (id: number) => request<DocumentDetail>(`/api/documents/${id}`),
  retry: (id: number) => post<DocumentSummary>(`/api/documents/${id}/retry`),
  remove: (id: number) => request<void>(`/api/documents/${id}`, { method: "DELETE" }),
  fileUrl: (id: number) => `/api/documents/${id}/file`,
  pageUrl: (id: number, page: number) => `/api/documents/${id}/pages/${page}`,

  /** Uploads one file with progress reporting (fetch can't report upload progress). */
  uploadOne: (companyId: number, file: File, onProgress?: (fraction: number) => void) =>
    new Promise<UploadResult>((resolve, reject) => {
      const form = new FormData();
      form.append("files", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/companies/${companyId}/documents`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress?.(e.loaded / e.total);
      };
      xhr.onload = () => {
        let body: unknown = null;
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          /* non-JSON error page */
        }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body as UploadResult);
        else reject(new ApiError(xhr.status, errorMessage(body, xhr.status)));
      };
      xhr.onerror = () =>
        reject(new ApiError(0, "Upload failed. Check the connection and try again."));
      xhr.send(form);
    }),

  /** Uploads files one request each, so the web proxy never buffers a whole batch.
   * A file that fails to upload is reported as rejected instead of stopping the batch. */
  upload: async (companyId: number, files: File[], onProgress?: (fraction: number) => void) => {
    const total = files.reduce((sum, f) => sum + f.size, 0) || 1;
    const merged: UploadResult = { documents: [], rejected: [] };
    let done = 0;
    for (const file of files) {
      if (file.size > MAX_UPLOAD_BYTES) {
        merged.rejected.push({
          filename: file.name,
          reason: `The file is larger than ${MAX_UPLOAD_MB} MB.`,
        });
        done += file.size;
        continue;
      }
      try {
        const result = await documentsApi.uploadOne(companyId, file, (f) =>
          onProgress?.((done + f * file.size) / total),
        );
        merged.documents.push(...result.documents);
        merged.rejected.push(...result.rejected);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) throw e;
        merged.rejected.push({
          filename: file.name,
          reason: e instanceof Error ? e.message : "Upload failed.",
        });
      }
      done += file.size;
      onProgress?.(done / total);
    }
    return merged;
  },
};

// -- vouchers -----------------------------------------------------------------------

export type VoucherStatus =
  | "pending"
  | "extracting"
  | "needs_review"
  | "ready"
  | "posting"
  | "posted"
  | "post_failed"
  | "rejected";

/** Decimals arrive as strings ("11800.00"); dates as "YYYY-MM-DD". */
export type InvoiceParty = {
  name: string | null;
  gstin: string | null;
  gstin_valid: boolean;
  state_code: string | null;
  state: string | null;
  address: string | null;
};

export type InvoiceLine = {
  description: string;
  hsn_sac: string | null;
  quantity: string | null;
  unit: string | null;
  rate: string | null;
  taxable_value: string | null;
  gst_rate: string | null;
};

export type Invoice = {
  is_invoice: boolean;
  document_type: string;
  invoice_number: string | null;
  invoice_date: string | null;
  seller: InvoiceParty;
  buyer: InvoiceParty;
  place_of_supply_code: string | null;
  reverse_charge: boolean;
  lines: InvoiceLine[];
  taxable_value: string | null;
  cgst: string;
  sgst: string;
  igst: string;
  cess: string;
  round_off: string;
  grand_total: string | null;
  confidence: Record<string, number>;
  notes: string[];
};

export type LedgerChoices = {
  direction: "purchase" | "sales" | null;
  party_ledger: string | null;
  create_party_ledger: boolean;
  item_ledger: string | null;
};

export type LedgerMatch = {
  ledger: string | null;
  method: string;
  score: number;
  candidates: string[];
};

export type ProposedLedger = {
  name: string;
  parent_group: string;
  gstin: string | null;
  state: string | null;
};

export type VoucherEntry = {
  ledger: { name: string; proposed: ProposedLedger | null };
  side: "dr" | "cr";
  amount: string;
  is_party: boolean;
};

export type Accounting = {
  direction: "purchase" | "sales";
  direction_reason: string;
  voucher_kind: string;
  party: LedgerMatch;
  proposed_party: ProposedLedger | null;
  item: LedgerMatch;
  tax_ledgers: Record<string, string>;
  transaction: { id: string; date: string; entries: VoucherEntry[] } | null;
  problems: { code: string; message: string; field: string | null }[];
};

export type Issue = {
  code: string;
  severity: "error" | "warning";
  message: string;
  field: string | null;
};

export type Voucher = {
  id: number;
  company_id: number;
  document_id: number;
  voucher_uid: string;
  status: VoucherStatus;
  source: "ai" | "manual";
  extraction_error: string | null;
  model: string | null;
  cost_usd: number;
  invoice: Invoice;
  choices: LedgerChoices;
  accounting: Accounting | null;
  issues: Issue[];
  confidence: number;
  invoice_number: string | null;
  party_name: string | null;
  voucher_kind: string | null;
  grand_total: number | null;
  posted_at: string | null;
  external_id: string | null;
  post_error: string | null;
  updated_at: string;
};

export const vouchersApi = {
  forDocument: (documentId: number) => request<Voucher>(`/api/documents/${documentId}/voucher`),
  list: (companyId: number, status?: VoucherStatus[]) => {
    const params = new URLSearchParams();
    status?.forEach((s) => params.append("status", s));
    const qs = params.size ? `?${params}` : "";
    return request<Voucher[]>(`/api/companies/${companyId}/vouchers${qs}`);
  },
  update: (id: number, invoice: Invoice, choices: LedgerChoices) =>
    request<Voucher>(`/api/vouchers/${id}`, {
      method: "PUT",
      body: JSON.stringify({ invoice, choices }),
    }),
  post: (id: number) => post<Voucher>(`/api/vouchers/${id}/post`),
  postReady: (companyId: number) =>
    post<{ posted: number[]; failed: { voucher_id: number; error: string }[] }>(
      `/api/companies/${companyId}/vouchers/post-ready`,
    ),
  reject: (id: number, reason?: string) => post<Voucher>(`/api/vouchers/${id}/reject`, { reason }),
  reopen: (id: number) => post<Voucher>(`/api/vouchers/${id}/reopen`),
  extractAgain: (id: number) => post<Voucher>(`/api/vouchers/${id}/extract`),
};

// -- phase 7: team, settings, backups, activity -----------------------------------------

/** One AI service the office can read documents with, and how far it is set up. */
export type AiService = {
  id: string;
  name: string;
  /** e.g. "Gemini API key" */
  key_name: string;
  /** Where to get a key. */
  key_help: string;
  /** A local server may need no key. */
  key_optional: boolean;
  /** The office enters the service's address. */
  custom_base_url: boolean;
  /** Claude only. */
  supports_effort: boolean;
  /** false: reads text only, so scanned bills and photos can't be read. */
  reads_images: boolean;
  /** Ready to read: a key (if needed), a model and an address (if needed). */
  configured: boolean;
  source: "settings" | "env" | null;
  key_hint: string | null;
  model: string;
  /** Suggestions; any model name the service knows is accepted (Claude: only these). */
  models: string[];
  base_url: string | null;
};

/** configured, source, key_hint, model and models describe the service in use. */
export type AiSettings = {
  /** id of the service in use */
  provider: string;
  configured: boolean;
  source: "settings" | "env" | null;
  key_hint: string | null;
  model: string;
  effort: string;
  models: string[];
  efforts: string[];
  services: AiService[];
};

export type OfficeSettings = {
  tally_url: string;
  tally_url_source: "settings" | "env";
  ai: AiSettings;
  /** Documents queued to be read because of this change (PUT only; 0 otherwise). */
  requeued: number;
  /** Anything more the server has to say about the change (PUT only), in plain words. */
  notice?: string | null;
};

/**
 * Only the fields present are changed. ai_api_key, ai_model and ai_base_url belong to the
 * service in ai_provider (else the service in use); "" removes a saved key or resets a model or
 * address. Saving a key puts its service in use once it can read invoices, or when the service
 * in use can't; a key in another service's own format is refused (422) with what to choose.
 * ai_provider sent alone puts that service in use; with anything else it only names whose
 * model, address or key is changed.
 */
export type OfficeSettingsUpdate = Partial<{
  tally_url: string;
  anthropic_api_key: string;
  claude_model: string;
  claude_effort: string;
  ai_provider: string;
  ai_api_key: string;
  ai_model: string;
  ai_base_url: string;
}>;

/** The models a service's key can use; detail says why the list couldn't be fetched. */
export type AiModels = { models: string[]; detail: string | null };

export type CheckResult = { ok: boolean; detail: string };

export type Backup = {
  name: string;
  size_bytes: number;
  created_at: string;
  reason: "scheduled" | "manual" | "before_migration";
};

export type ActivityItem = {
  id: number;
  created_at: string;
  actor_id: number | null;
  /** null means the system (background worker or scheduled job). */
  actor_name: string | null;
  action: string;
  entity_type: string;
  entity_id: string;
  company_id: number | null;
  summary: string;
  document_id: number | null;
  data: Record<string, unknown>;
};

export type ActivityPage = { items: ActivityItem[]; next_before_id: number | null };

export type FieldChange = { field: string; label: string; before: unknown; after: unknown };

export type PostingRecord = {
  id: number;
  kind: "ledger" | "voucher";
  reference: string;
  success: boolean;
  error: string | null;
  request_payload: string;
  response_payload: string | null;
  created_at: string;
};

export type HistoryItem = {
  at: string;
  actor_name: string | null;
  action: string;
  summary: string;
  changes: FieldChange[];
  posting: PostingRecord | null;
};

export type VoucherHistory = { voucher_id: number; document_id: number; items: HistoryItem[] };

export type NewUser = { email: string; full_name: string; password: string; role: Role };

export const teamApi = {
  list: () => request<User[]>("/api/users"),
  create: (data: NewUser) => post<User>("/api/users", data),
  update: (id: number, data: Partial<Pick<User, "full_name" | "role" | "is_active">>) =>
    patch<User>(`/api/users/${id}`, data),
  resetPassword: (id: number, password: string) =>
    post<void>(`/api/users/${id}/password`, { password }),
  changeOwnPassword: (current_password: string, new_password: string) =>
    post<void>("/api/auth/password", { current_password, new_password }),
};

export const settingsApi = {
  get: () => request<OfficeSettings>("/api/settings"),
  update: (data: OfficeSettingsUpdate) =>
    request<OfficeSettings>("/api/settings", { method: "PUT", body: JSON.stringify(data) }),
  /** Tests the service in use. */
  testAi: () => post<CheckResult>("/api/settings/test-ai"),
  aiModels: (service: string) =>
    request<AiModels>(`/api/settings/ai-models?service=${encodeURIComponent(service)}`),
  testTally: () => post<CheckResult>("/api/settings/test-tally"),
};

export const backupsApi = {
  list: () => request<Backup[]>("/api/backups"),
  create: () => post<Backup>("/api/backups"),
  downloadUrl: (name: string) => `/api/backups/${encodeURIComponent(name)}`,
};

export const activityApi = {
  company: (
    companyId: number,
    filters: {
      entity_type?: string;
      action_prefix?: string;
      before_id?: number;
      limit?: number;
    } = {},
  ) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== "") params.set(key, String(value));
    }
    const qs = params.size ? `?${params}` : "";
    return request<ActivityPage>(`/api/companies/${companyId}/activity${qs}`);
  },
  /** Office-wide events with no company: sign-ins, team, settings, backups (admins). */
  office: (filters: { entity_type?: string; action_prefix?: string; before_id?: number } = {}) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== "") params.set(key, String(value));
    }
    const qs = params.size ? `?${params}` : "";
    return request<ActivityPage>(`/api/activity${qs}`);
  },
  voucherHistory: (voucherId: number) =>
    request<VoucherHistory>(`/api/vouchers/${voucherId}/history`),
};
