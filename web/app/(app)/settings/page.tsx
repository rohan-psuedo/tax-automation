"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, type ReactNode, useState } from "react";
import { Button, ErrorNote, Field, Ident, Input } from "@/components/ui";
import {
  type AiService,
  type AiSettings,
  type CheckResult,
  type OfficeSettings,
  type OfficeSettingsUpdate,
  settingsApi,
} from "@/lib/api";

// The same rule the backend applies: scheme, host and optional port, nothing after.
const TALLY_URL = /^https?:\/\/[^\s/]+(:\d+)?\/?$/;

const EXAMPLE_URL = "http://192.168.1.20:9000";

/**
 * Tally shows its address without a scheme ("localhost:9000"), so people often type it that
 * way: add http:// for them. A scheme that is there is only lower-cased.
 */
function withScheme(value: string) {
  if (value === "") return value;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) {
    return value.replace(/^[a-z][a-z0-9+.-]*:\/\//i, (scheme) => scheme.toLowerCase());
  }
  return `http://${value}`;
}

/** What is wrong with an address, in words a clerk can act on; null when it is fine. */
function tallyUrlProblem(value: string): string | null {
  if (value === "") return `Enter the Tally address, for example ${EXAMPLE_URL}.`;
  if (!/^https?:\/\//.test(value)) {
    return `Start the address with http://, for example ${EXAMPLE_URL}.`;
  }
  if (TALLY_URL.test(value)) return null;
  if (/^https?:\/\/[^\s/]+(:\d+)?\/./.test(value)) {
    return `Remove everything after the port number, so the address looks like ${EXAMPLE_URL}.`;
  }
  return `Enter the computer's name or network address, a colon and the port, like ${EXAMPLE_URL}.`;
}

const SELECT =
  "h-9 w-full rounded-[3px] border border-rule-strong bg-sheet px-2 text-sm focus:border-ledger focus:outline-none disabled:bg-paper";

const OK_NOTE = "border-l-2 border-ledger bg-ledger-tint px-3 py-2 text-sm";

export default function TallyAndAiSettingsPage() {
  const settings = useQuery({ queryKey: ["settings"], queryFn: settingsApi.get });

  if (settings.error) {
    return (
      <div className="mt-6">
        <ErrorNote>{settings.error.message}</ErrorNote>
      </div>
    );
  }
  if (!settings.data) {
    return <p className="mt-6 text-sm text-ink-soft">Loading settings…</p>;
  }
  return (
    <div className="max-w-2xl">
      <TallySection settings={settings.data} />
      {settings.data.ai.services?.length ? (
        <AiSection ai={settings.data.ai} />
      ) : (
        <div className="mt-10">
          <ErrorNote>
            The app&apos;s server didn&apos;t list any AI services. Restart the app and open this
            page again.
          </ErrorNote>
        </div>
      )}
    </div>
  );
}

function TallySection({ settings }: { settings: OfficeSettings }) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState(settings.tally_url);
  const [baseline, setBaseline] = useState(settings.tally_url);
  const [problem, setProblem] = useState<string | null>(null);

  // The saved address changed (saved here, or by another administrator): show it.
  if (baseline !== settings.tally_url) {
    setBaseline(settings.tally_url);
    setUrl(settings.tally_url);
  }

  const candidate = withScheme(url.trim());
  const dirty = candidate !== settings.tally_url;

  const test = useMutation({ mutationFn: settingsApi.testTally });
  const save = useMutation({
    mutationFn: (tally_url: string) => settingsApi.update({ tally_url }),
    onSuccess: (data) => {
      queryClient.setQueryData(["settings"], data);
      queryClient.invalidateQueries({ queryKey: ["tally-status"] });
      test.reset();
    },
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const found = tallyUrlProblem(candidate);
    if (found) {
      setProblem(found);
      return;
    }
    setUrl(candidate);
    save.mutate(candidate);
  }

  return (
    <section aria-labelledby="tally-title" className="mt-6">
      <h2 id="tally-title" className="text-sm font-semibold">
        Tally
      </h2>
      <p className="mt-0.5 text-xs text-ink-soft">
        Where the app finds TallyPrime to copy ledgers and post entries.
      </p>

      <form onSubmit={onSubmit} noValidate className="mt-4">
        <Field
          label="Tally address"
          hint="The computer running TallyPrime and the port set in Tally (Alt+Z › Configure › Client/Server Configuration), usually 9000. If Tally runs on the same computer as this app, use 127.0.0.1 rather than localhost; it answers faster. Otherwise use that computer's network address, like 192.168.1.20."
        >
          <Input
            value={url}
            onChange={(e) => {
              setUrl(e.target.value);
              setProblem(null);
            }}
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            aria-invalid={problem ? true : undefined}
            placeholder="http://127.0.0.1:9000"
            className="max-w-sm font-mono"
          />
        </Field>
        <p className="mt-1.5 text-xs text-ink-soft">
          {settings.tally_url_source === "settings"
            ? "Saved on this screen."
            : "Comes from the app's configuration file. Saving an address here takes its place."}
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button type="submit" disabled={!dirty || save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
          <Button
            type="button"
            variant="secondary"
            onClick={() => test.mutate()}
            disabled={dirty || test.isPending}
          >
            {test.isPending ? "Testing…" : "Test connection"}
          </Button>
          {dirty && (
            <span className="text-xs text-ink-soft">Save the new address first, then test it.</span>
          )}
        </div>
      </form>

      <Notes>
        {problem && <ErrorNote>{problem}</ErrorNote>}
        {save.error && <ErrorNote>{save.error.message}</ErrorNote>}
        {save.isSuccess && !dirty && (
          <p role="status" className={OK_NOTE}>
            Saved. The app now uses this address for Tally.
          </p>
        )}
        {test.error && <ErrorNote>{test.error.message}</ErrorNote>}
        {test.data && (
          <CheckNote result={test.data} ok="Tally answered." failed="Couldn't reach Tally." />
        )}
      </Notes>
    </section>
  );
}

// -- invoice reading (AI) ----------------------------------------------------------------

/** The server accepts only the Claude models it lists, so Claude's model is a fixed choice. */
const LISTED_MODELS_ONLY = new Set(["anthropic"]);

/**
 * Key formats, as in backend/app/extraction/services.py detect(): a stricter pattern is tried
 * first, then the longest matching prefix wins ("sk-ant-" over "sk-"). A strong format is used
 * by that service only. A weak one is shared: many OpenAI-compatible services (Moonshot,
 * SiliconFlow, LiteLLM) issue "sk-" keys too, and Alibaba's look exactly like DeepSeek's.
 */
const KEY_FORMATS: Record<string, { pattern?: RegExp; prefixes?: string[]; strong: boolean }> = {
  anthropic: { prefixes: ["sk-ant-"], strong: true },
  gemini: { prefixes: ["AIza", "AQ."], strong: true },
  openai: { prefixes: ["sk-proj-", "sk-svcacct-", "sk-admin-", "sk-"], strong: false },
  openrouter: { prefixes: ["sk-or-"], strong: true },
  groq: { prefixes: ["gsk_"], strong: true },
  xai: { prefixes: ["xai-"], strong: true },
  deepseek: { pattern: /^sk-[0-9a-f]{32}$/, strong: false },
};

/** A backend/.env line as the server recognises one: GEMINI_API_KEY=AIza… */
const ENV_LINE = /^[A-Z][A-Z0-9_]*_(?:KEY|TOKEN)\s*=\s*([^=\s]\S*)$/;

const EXAMPLE_AI_URL = "http://192.168.1.30:11434/v1";

type AiActionKind = "key" | "remove" | "choices" | "use";

type AiAction = {
  kind: AiActionKind;
  service: AiService;
  changes: OfficeSettingsUpdate;
  /** id of the service in use when the change was made */
  inUse: string;
};

/**
 * Saves a change to one service in one request. With a model, an address or a removed key,
 * ai_provider only names the service they belong to; the server changes the service in use
 * only for "Use … for reading" and for a new key, so nothing has to be put back afterwards.
 */
function saveAi({ changes }: AiAction): Promise<OfficeSettings> {
  return settingsApi.update(changes);
}

function AiSection({ ai }: { ai: AiSettings }) {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState(ai.provider);
  const [key, setKey] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const inUse = findService(ai, ai.provider);
  const selected = findService(ai, selectedId);
  const choices = useChoiceDrafts(selected, ai.effort);
  const unsaved = choices.dirty || key.trim() !== "";

  const test = useMutation({ mutationFn: settingsApi.testAi });
  const save = useMutation({
    mutationFn: saveAi,
    onSuccess: (data, action) => {
      queryClient.setQueryData(["settings"], data);
      test.reset();
      if (action.kind === "key") setKey("");
      setNotice(aiNotice(action, data));
    },
  });
  const pending = save.isPending ? (save.variables?.kind ?? null) : null;

  function run(kind: AiActionKind, changes: OfficeSettingsUpdate) {
    setNotice(null); // only the latest outcome is shown
    save.mutate({ kind, service: selected, changes, inUse: ai.provider });
  }

  /** Only the administrator changes the service: the key note offers it, never does it. */
  function onChooseService(id: string) {
    setSelectedId(id);
    if (save.isError) save.reset(); // the refusal it answered no longer applies
  }

  function onSaveKey(e: FormEvent) {
    e.preventDefault();
    const value = key.trim();
    if (value) run("key", { ai_provider: selected.id, ai_api_key: value });
  }

  function onRemoveKey() {
    const stopsReading = selected.id === ai.provider && !selected.key_optional;
    const question = `Remove the saved ${selected.key_name}?${
      stopsReading ? " Invoices can't be read without a key." : ""
    }`;
    if (window.confirm(question)) run("remove", { ai_provider: selected.id, ai_api_key: "" });
  }

  function onSaveChoices(e: FormEvent) {
    e.preventDefault();
    run("choices", choices.changes);
  }

  const testNote = !ai.configured
    ? `Nothing to test yet: ${shortName(inUse)}, the service in use, is not set up.`
    : unsaved
      ? "Save your changes first, then test them."
      : selected.id !== inUse.id
        ? `This tests ${shortName(inUse)}, the service in use.`
        : null;

  return (
    <section aria-labelledby="ai-title" className="mt-10 border-t border-rule pt-6">
      <h2 id="ai-title" className="text-sm font-semibold">
        Invoice reading (AI)
      </h2>
      <PrivacyNote inUse={inUse} />

      <ServicePicker
        ai={ai}
        selected={selected}
        onSelect={setSelectedId}
        onUse={() => run("use", { ai_provider: selected.id })}
        pending={pending}
      />

      <KeyForm
        service={selected}
        inUse={inUse}
        services={ai.services}
        value={key}
        onChange={setKey}
        onChoose={onChooseService}
        onSubmit={onSaveKey}
        onRemove={onRemoveKey}
        pending={pending}
      />

      <ChoicesForm
        service={selected}
        drafts={choices}
        efforts={ai.efforts}
        onSubmit={onSaveChoices}
        pending={pending}
      />

      <div className="mt-6 flex flex-wrap items-center gap-2 border-t border-rule pt-4">
        <Button
          type="button"
          variant="secondary"
          onClick={() => test.mutate()}
          disabled={!ai.configured || unsaved || test.isPending}
        >
          {test.isPending ? "Testing…" : "Test AI connection"}
        </Button>
        {testNote && <span className="text-xs text-ink-soft">{testNote}</span>}
      </div>

      <Notes>
        {save.error && <ErrorNote>{save.error.message}</ErrorNote>}
        {notice && (
          <p role="status" className={OK_NOTE}>
            {notice}
          </p>
        )}
        {test.error && <ErrorNote>{test.error.message}</ErrorNote>}
        {test.data && (
          <CheckNote
            result={test.data}
            ok="The AI connection works."
            failed="The AI connection didn't work."
          />
        )}
      </Notes>
    </section>
  );
}

function PrivacyNote({ inUse }: { inUse: AiService }) {
  const where = !inUse.custom_base_url ? (
    `the ${shortName(inUse)} API`
  ) : inUse.base_url ? (
    <>
      the service at <Ident>{inUse.base_url}</Ident>
    </>
  ) : (
    "the AI service you set up below"
  );
  return (
    <p className="mt-2 border-l-2 border-rule-strong bg-sheet px-3 py-2 text-xs leading-relaxed text-ink-soft">
      To read an invoice, the app sends the file to {where}, which reads it.{" "}
      {inUse.source === "env"
        ? "The key in use now is read from the app's configuration file on the computer the app runs on. Keys you save here are stored encrypted on that computer instead, and are never shown again."
        : "Keys you save here are stored encrypted on the computer the app runs on, and are never shown again."}
    </p>
  );
}

function ServicePicker({
  ai,
  selected,
  onSelect,
  onUse,
  pending,
}: {
  ai: AiSettings;
  selected: AiService;
  onSelect: (id: string) => void;
  onUse: () => void;
  pending: AiActionKind | null;
}) {
  const inUse = findService(ai, ai.provider);
  return (
    <div className="mt-4">
      <Field
        label="AI service"
        hint="The service your API key is from. Choosing one here changes nothing until you save."
      >
        <select
          value={selected.id}
          onChange={(e) => onSelect(e.target.value)}
          className={`${SELECT} max-w-sm`}
        >
          {ai.services.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
              {s.id === ai.provider ? " – in use" : s.configured ? " – set up" : ""}
            </option>
          ))}
        </select>
      </Field>

      <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        <span
          className={`inline-block rounded-[3px] px-1.5 py-0.5 text-xs font-medium ${
            selected.configured ? "bg-ledger-tint text-ledger" : "bg-amber-tint text-ink"
          }`}
        >
          {selected.configured ? "Set up" : "Not set up"}
        </span>
        <span className="text-ink-soft">
          <KeyStatus service={selected} />{" "}
          {selected.id === inUse.id
            ? selected.configured
              ? "Reads your invoices."
              : "It is the service chosen to read invoices."
            : `Not in use; the service in use is ${shortName(inUse)}.`}
        </span>
      </div>

      {!selected.reads_images && (
        <p className="mt-3 border-l-2 border-amber bg-amber-tint px-3 py-2 text-xs leading-relaxed">
          {capital(shortName(selected))} reads only the text of a document, so scanned bills and
          photos can&apos;t be read with it.
        </p>
      )}

      {selected.configured && selected.id !== inUse.id && (
        <div className="mt-3">
          <Button type="button" variant="secondary" onClick={onUse} disabled={pending !== null}>
            {pending === "use" ? "Switching…" : `Use ${shortName(selected)} for reading`}
          </Button>
        </div>
      )}
    </div>
  );
}

/** Where the selected service's key comes from, or what it still needs. */
function KeyStatus({ service }: { service: AiService }) {
  const hint = service.key_hint && <KeyHint hint={service.key_hint} />;
  const missing = service.configured ? null : ` ${nextStep(service)}`;
  if (service.source === "settings") {
    return (
      <>
        Using the key saved here{hint}.{missing}
      </>
    );
  }
  if (service.source === "env") {
    return (
      <>
        Using the key from the app&apos;s configuration file{hint}. A key saved here takes its
        place.{missing}
      </>
    );
  }
  return <>{service.configured ? "No key needed." : nextStep(service)}</>;
}

function KeyForm({
  service,
  inUse,
  services,
  value,
  onChange,
  onChoose,
  onSubmit,
  onRemove,
  pending,
}: {
  service: AiService;
  inUse: AiService;
  services: AiService[];
  value: string;
  onChange: (value: string) => void;
  onChoose: (id: string) => void;
  onSubmit: (e: FormEvent) => void;
  onRemove: () => void;
  pending: AiActionKind | null;
}) {
  const found = detectService(value, services);
  // The server refuses a key in another service's own format, so there is no outcome to tell;
  // "Other" may be a gateway that takes any service's keys.
  const refused = found?.strong && found.service.id !== service.id && !service.custom_base_url;
  const saveNote = value.trim() && !refused ? keySaveNote(service, inUse) : null;
  const label = service.source
    ? `New ${service.key_name}`
    : service.key_optional
      ? `${service.key_name} (if the service needs one)`
      : service.key_name;
  return (
    <form onSubmit={onSubmit} className="mt-5">
      <Field label={label} hint={service.key_help}>
        {/* Browsers ignore autocomplete="off" on password boxes and may fill in the sign-in
            password; "new-password" and the password-manager opt-outs keep them away. */}
        <Input
          type="password"
          name="ai-api-key"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          autoComplete="new-password"
          data-1p-ignore=""
          data-lpignore="true"
          data-bwignore=""
          data-form-type="other"
          spellCheck={false}
          className="max-w-md font-mono"
        />
      </Field>
      <KeyNote found={found} service={service} onChoose={onChoose} pending={pending} />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button type="submit" disabled={!value.trim() || pending !== null}>
          {pending === "key" ? "Saving…" : "Save key"}
        </Button>
        {service.source === "settings" && (
          <Button
            type="button"
            variant="quiet"
            className="text-red-ink hover:bg-red-tint hover:text-red-ink"
            onClick={onRemove}
            disabled={pending !== null}
          >
            {pending === "remove" ? "Removing…" : "Remove saved key"}
          </Button>
        )}
        {saveNote && <span className="text-xs text-ink-soft">{saveNote}</span>}
      </div>
    </form>
  );
}

/**
 * Which service a typed key looks like it is from. The choice stays with the administrator: a
 * wrong guess would send the office's invoices to a service it never chose, so only a format no
 * other service uses is offered as a one-click switch, and a shared one is only a caution.
 */
function KeyNote({
  found,
  service,
  onChoose,
  pending,
}: {
  found: DetectedService | null;
  service: AiService;
  onChoose: (id: string) => void;
  pending: AiActionKind | null;
}) {
  if (!found) return null;
  const name = shortName(found.service);
  const looks = `This looks like ${/^[aeiou]/i.test(name) ? "an" : "a"} ${name} key.`;
  if (found.service.id === service.id) {
    // A shared format proves nothing about the service chosen, so it gets no confirmation.
    if (!found.strong) return null;
    return (
      <p role="status" className="mt-1.5 text-xs text-ledger">
        {looks}
      </p>
    );
  }
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 border-l-2 border-amber bg-amber-tint px-3 py-2 text-xs leading-relaxed">
      <p role="status">
        {found.strong
          ? looks
          : `This might be a key for ${name}. Choose the service it belongs to.`}
      </p>
      {found.strong && (
        <Button
          type="button"
          variant="secondary"
          className="h-7 px-2.5 text-xs"
          onClick={() => onChoose(found.service.id)}
          disabled={pending !== null}
        >
          Use {name}
        </Button>
      )}
    </div>
  );
}

/**
 * What saving a key does to the service in use, said before it happens. The server puts the
 * key's service in use only when it can read invoices with it, or when the one in use can't.
 */
function keySaveNote(selected: AiService, inUse: AiService): string | null {
  if (selected.id === inUse.id) return null;
  const short = shortName(selected);
  if (!inUse.configured) return `Saving the key puts ${short} in use to read invoices.`;
  const readyOnceSaved = !selected.custom_base_url || (!!selected.base_url && !!selected.model);
  return readyOnceSaved
    ? `Saving the key also puts ${short} in use to read invoices, in place of ${shortName(inUse)}.`
    : `${capital(shortName(inUse))} stays in use until ${short} is set up.`;
}

type Choices = { model: string; baseUrl: string; effort: string };

/** Addresses are compared without the trailing slash the server drops. */
const normalUrl = (url: string) => url.trim().replace(/\/+$/, "");

/**
 * The model, address and effort being edited for one service. They start from the saved values
 * and start over when those change (saved here or by another administrator) or when another
 * service is chosen.
 */
function useChoiceDrafts(service: AiService, savedEffort: string) {
  const saved: Choices = {
    model: service.model,
    baseUrl: service.base_url ?? "",
    effort: savedEffort,
  };
  const savedKey = [service.id, saved.model, saved.baseUrl, saved.effort].join("\n");
  const [baseline, setBaseline] = useState(savedKey);
  const [draft, setDraft] = useState(saved);
  if (baseline !== savedKey) {
    setBaseline(savedKey);
    setDraft(saved);
  }

  const changes: OfficeSettingsUpdate = {};
  if (draft.model.trim() !== saved.model) changes.ai_model = draft.model.trim();
  if (service.custom_base_url && normalUrl(draft.baseUrl) !== normalUrl(saved.baseUrl)) {
    changes.ai_base_url = normalUrl(draft.baseUrl);
  }
  if (changes.ai_model !== undefined || changes.ai_base_url !== undefined) {
    changes.ai_provider = service.id;
  }
  if (service.supports_effort && draft.effort !== saved.effort) {
    changes.claude_effort = draft.effort;
  }
  return {
    draft,
    update: (patch: Partial<Choices>) => setDraft((current) => ({ ...current, ...patch })),
    changes,
    dirty: Object.keys(changes).length > 0,
  };
}

function ChoicesForm({
  service,
  drafts,
  efforts,
  onSubmit,
  pending,
}: {
  service: AiService;
  drafts: ReturnType<typeof useChoiceDrafts>;
  efforts: string[];
  onSubmit: (e: FormEvent) => void;
  pending: AiActionKind | null;
}) {
  const { draft, update } = drafts;
  const effortChoices = efforts.includes(draft.effort) ? efforts : [draft.effort, ...efforts];
  return (
    <form onSubmit={onSubmit} className="mt-6">
      {service.custom_base_url && (
        <div className="mb-4">
          <Field
            label="Service address"
            hint={`The service's API address, as its instructions give it; it usually ends in /v1. For Ollama on another computer, for example ${EXAMPLE_AI_URL}.`}
          >
            <Input
              value={draft.baseUrl}
              onChange={(e) => update({ baseUrl: e.target.value })}
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              placeholder={EXAMPLE_AI_URL}
              className="max-w-md font-mono"
            />
          </Field>
        </div>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <div className={service.supports_effort ? undefined : "sm:col-span-2"}>
          <ModelField
            service={service}
            value={draft.model}
            onChange={(model) => update({ model })}
          />
        </div>
        {service.supports_effort && (
          <Field
            label="Effort"
            hint="How hard the AI thinks about each invoice. Higher is slower and costs more."
          >
            <select
              value={draft.effort}
              onChange={(e) => update({ effort: e.target.value })}
              className={SELECT}
            >
              {effortChoices.map((level) => (
                <option key={level} value={level}>
                  {EFFORT_LABEL[level] ?? level}
                </option>
              ))}
            </select>
          </Field>
        )}
      </div>
      <div className="mt-3">
        <Button type="submit" variant="secondary" disabled={!drafts.dirty || pending !== null}>
          {pending === "choices" ? "Saving…" : "Save changes"}
        </Button>
      </div>
    </form>
  );
}

/**
 * Claude: a choice of the models the server allows. Any other service: a free box with
 * suggestions, plus the models the saved key can use once the service is set up.
 */
function ModelField({
  service,
  value,
  onChange,
}: {
  service: AiService;
  value: string;
  onChange: (value: string) => void;
}) {
  const listedOnly = LISTED_MODELS_ONLY.has(service.id);
  const live = useQuery({
    // A new key or address can see other models.
    queryKey: ["ai-models", service.id, service.key_hint, service.base_url],
    queryFn: () => settingsApi.aiModels(service.id),
    enabled: service.configured && !listedOnly,
    staleTime: 10 * 60_000,
    retry: false,
  });

  if (listedOnly) {
    const models = service.models.includes(value) ? service.models : [value, ...service.models];
    return (
      <Field label="Model" hint={modelNote(value)}>
        <select value={value} onChange={(e) => onChange(e.target.value)} className={SELECT}>
          {models.map((m) => (
            <option key={m} value={m}>
              {modelName(m)}
            </option>
          ))}
        </select>
      </Field>
    );
  }

  const suggestions = Array.from(new Set([...service.models, ...(live.data?.models ?? [])]));
  const listId = `ai-models-${service.id}`;
  const asListed = `the model name exactly as ${shortName(service)} lists it.`;
  const hint = [
    suggestions.length ? `Pick a suggestion, or type ${asListed}` : `Type ${asListed}`,
    live.data?.detail ?? live.error?.message,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <Field label="Model" hint={hint}>
      <Input
        list={listId}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete="off"
        spellCheck={false}
        placeholder={service.models[0] ?? "llama3.1"}
        className="max-w-md font-mono"
      />
      <datalist id={listId}>
        {suggestions.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
    </Field>
  );
}

function findService(ai: AiSettings, id: string): AiService {
  return ai.services.find((s) => s.id === id) ?? ai.services[0];
}

/** The service a key's format points to; null when it can't be told. */
type DetectedService = { service: AiService; strong: boolean };

/**
 * The key without what is often pasted along with it, as the server strips it: the variable
 * name of a backend/.env line, then surrounding quotes.
 */
function cleanKey(key: string): string {
  let value = key.trim();
  const named = ENV_LINE.exec(value);
  if (named) value = named[1].trim();
  const quote = value[0];
  if (value.length >= 2 && (quote === '"' || quote === "'") && value.at(-1) === quote) {
    value = value.slice(1, -1).trim();
  }
  return value;
}

/** The service a key's format points to; null when it can't be told. */
function detectService(key: string, services: AiService[]): DetectedService | null {
  const value = cleanKey(key);
  if (!value) return null;
  let found = services.find((s) => KEY_FORMATS[s.id]?.pattern?.test(value)) ?? null;
  if (!found) {
    let longest = 0;
    for (const s of services) {
      for (const prefix of KEY_FORMATS[s.id]?.prefixes ?? []) {
        if (value.startsWith(prefix) && prefix.length > longest) {
          found = s;
          longest = prefix.length;
        }
      }
    }
  }
  return found && { service: found, strong: KEY_FORMATS[found.id].strong };
}

/** "Gemini (Google)" -> "Gemini", for use inside a sentence. */
function shortName(service: AiService) {
  if (service.custom_base_url) return "the OpenAI-compatible service";
  return service.name.replace(/\s*\(.*\)$/, "");
}

function capital(text: string) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** What a service still needs before it can read invoices, as an instruction. */
function nextStep(service: AiService) {
  if (!service.source && !service.key_optional) return `Add its ${service.key_name} below.`;
  if (service.custom_base_url && !service.base_url) return "Enter its address and model below.";
  return "Enter a model below.";
}

function waitingText(requeued: number) {
  if (requeued <= 0) return "";
  const documents = requeued === 1 ? "1 document" : `${requeued} documents`;
  return `${documents} waiting to be read will be read now.`;
}

/** How to start reading with a service that isn't in use, once it is ready. */
function howToUse(service: AiService) {
  const short = shortName(service);
  const choose = `choose Use ${short} for reading above.`;
  return service.configured
    ? `To read invoices with ${short}, ${choose}`
    : `${nextStep(service)} Then ${choose}`;
}

/** The outcome of a saved change, from the server's answer, then anything the server adds. */
function aiNotice(action: AiAction, data: OfficeSettings): string {
  return [aiOutcome(action, data), data.notice, waitingText(data.requeued)]
    .filter(Boolean)
    .join(" ");
}

function aiOutcome({ kind, service, inUse: before }: AiAction, data: OfficeSettings): string {
  const now = findService(data.ai, service.id);
  const current = findService(data.ai, data.ai.provider);
  const name = capital(shortName(now));
  const inUse = current.id === now.id;
  const switched = current.id !== before;
  const waits = "Invoices wait to be read until then.";
  switch (kind) {
    case "key":
      // The server keeps a working service in use until the key's service can read too.
      if (!inUse) {
        const still = `${capital(shortName(current))} is still used to read invoices.`;
        return `Key saved for ${shortName(now)}. ${still} ${howToUse(now)}`;
      }
      if (!now.configured) {
        const chosen = switched ? ` ${name} is now chosen to read invoices.` : "";
        return `Key saved.${chosen} ${nextStep(now)} ${waits}`;
      }
      return switched
        ? `Key saved. ${name} is now used to read invoices.`
        : `Key saved. ${name} reads invoices with the new key.`;
    case "use":
      return `${name} is now used to read invoices.`;
    case "remove": {
      const removed =
        now.source === "env"
          ? "Saved key removed. The key from the app's configuration file is used again."
          : "Saved key removed.";
      if (!data.ai.configured) {
        return inUse || switched
          ? `${removed} Invoices wait to be read until you add a key.`
          : removed;
      }
      // With no service chosen, the first one that is set up takes over.
      return switched
        ? `${removed} ${capital(shortName(current))} is now used to read invoices.`
        : removed;
    }
    case "choices":
      if (!inUse) return `Saved. ${howToUse(now)}`;
      return now.configured
        ? "Saved. New invoices are read with these choices."
        : `Saved. ${nextStep(now)} ${waits}`;
  }
}

const EFFORT_LABEL: Record<string, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Maximum",
};

/** "claude-opus-5-5" -> "Claude Opus 5.5"; unknown ids are shown as they are. */
function modelName(id: string) {
  const m = /^claude-(opus|sonnet|haiku)-(\d+)-(\d+)/.exec(id);
  if (!m) return id;
  return `Claude ${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}`;
}

function modelNote(id: string) {
  if (id.includes("opus")) return "Opus is the most accurate, and costs the most per invoice.";
  if (id.includes("sonnet")) return "Sonnet is cheaper and faster, and a little less accurate.";
  if (id.includes("haiku")) return "Haiku is the cheapest and fastest, and the least accurate.";
  return "The Claude model that reads your invoices.";
}

function KeyHint({ hint }: { hint: string }) {
  return (
    <>
      {" ("}
      <Ident>{hint}</Ident>
      {")"}
    </>
  );
}

function CheckNote({ result, ok, failed }: { result: CheckResult; ok: string; failed: string }) {
  if (!result.ok) {
    return (
      <ErrorNote>
        {failed} {result.detail}
      </ErrorNote>
    );
  }
  return (
    <p role="status" className={OK_NOTE}>
      {ok} <span className="text-ink-soft">{result.detail}</span>
    </p>
  );
}

function Notes({ children }: { children: ReactNode }) {
  return <div className="mt-3 space-y-2 empty:hidden">{children}</div>;
}
