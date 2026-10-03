# Automation plan

From the zero-touch audit of 2026-10-03: 17 agents traced every step between a document arriving and the entry landing in Tally. They found 182 manual steps, each verified against the code. The full findings, with code references, are in [automation-audit.json](automation-audit.json).

Decisions (2026-10-03):
- Build all phases in order, checking in after each.
- No amount limit for automatic posting.
- A client switches to automatic posting after 20 clean posts in a row.
- New party ledgers are created automatically for GST-registered parties; unregistered parties only below ₹10,000.

## Summary

The app reads invoices well, but with the settings it ships with, nothing posts on its own. Every document needs a person twice: once to upload it into the right client, and once to open it and click Post.

Four things block zero-touch most:
1. **Routing.** Any warning holds an entry for review, including purely informational ones such as 'reverse charge booked' or 'new ledger will be created' (validation/engine.py:789). New companies also start with 'Review every entry' on and auto-post off.
2. **Ledgers.** New parties, name-only party matches, expense heads and missing tax or round-off ledgers all need a person. Most of these picks are never learned, and ledger sync only runs when someone clicks the button.
3. **Intake.** The only way in is a browser upload into a client chosen by hand, and only the first bill in each file is read.
4. **Coverage.** Several document types produce no entries at all: bank statements, receipts and payments, sales/purchase registers, TDS, stock items, GSTR-2B, challans, payroll and journals. They are probably the larger half of an office's keying.

Failure handling adds manual work as well: a short AI outage turns documents into hand typing, and a Tally hiccup turns entries into post_failed items that someone must re-post. One fix is already in: documents waiting for an AI key are re-queued when the key is saved (services/vouchers.py:752, called from api/settings.py:133). Exhausted credit and short outages are not covered yet.

The plan:
- **Phase 1:** a clean invoice reaches Tally with zero clicks once it is in the app.
- **Phase 2:** nobody uploads or sorts files.
- **Phase 3:** most problem invoices are fixed without a person.
- **Phase 4:** adds the missing document types.
- **Phase 5:** the long tail.

Phase 3 comes before the new document types because its items are small and hit invoices every day. If bank statements dominate the office's keying, start 4.2 alongside Phase 2.

Honest limit: 'no manual work at all' cannot be reached. Paper still has to be scanned, TallyPrime has to be running with the client's books loaded, and real judgement calls stay with a CA. The realistic target is a short daily exceptions list that tells you exactly what to decide, and in most cases each decision is needed only once per party or layout.

## A clean invoice today

With default settings, a clean purchase invoice from a supplier already in Tally needs 3 human actions, plus standing set-up work:
1. Open that client's Documents tab and drop the file in. This is shared across a batch.
2. Open the entry. It always lands in 'Needs review', because always_review defaults to True (models/company.py:29, validation/engine.py:789).
3. Check it against the image and click 'Post to Tally'. auto_post defaults to False (models/company.py:33).

Common extras on an otherwise clean bill:
4. The first bill from a new party needs 'Create new: <party>' picked in the supplier dropdown (party_ledger_needs_approval error; auto_create_ledgers defaults to False).
5. A party matched only by a similar name has to be confirmed on every bill (weak_party_match), because the pick is never remembered.
6. A company with several purchase ledgers and no learned mapping needs the purchase ledger confirmed (weak_item_ledger).

Standing set-up work:
- Double-click start.bat every morning.
- Keep TallyPrime open with that client's company loaded.
- Click 'Sync ledgers from Tally' after adding a company and whenever ledgers change in Tally.

Even after an admin turns review off and auto-post on, the upload remains, and new-party bills still stop for review.

## Phase 1 - Clean invoices post themselves

Once a document is in the app, a correctly read purchase or sales invoice reaches Tally with no clicks, and anything that cannot goes to one exceptions list with a clear reason. Target after this phase: 1 action per clean invoice (the upload), and 0 once Phase 2 lands. Order of shipping: 1.12 (safety flags), the expense-head fix in 1.4 and 1.16 (ITC) must be live before 1.2 switches auto-post on by default.

### 1.1 Only real doubts hold an entry: add an 'info' level and route on blocking issues (M)

**Removes:** Opening an entry and clicking Post when its only issues describe what the engine already did. Examples: reverse charge already booked, credit/debit note type already set, a new ledger that will be created, CGST/SGST differing by Re 1 or less, line sum differing while the header totals reconcile, and an invoice over 365 days old that is still inside the books period.

**How:** validation/types.py:5: add 'info' to Severity. validation/engine.py:789: route on blocking issues only, i.e. needs_review = always_review or any(i.severity != 'info'), plus a per-code policy. Turn these into info when the evidence holds:
- reverse_charge (engine.py:565-574), when accounting resolved the *_rcm ledgers (accounting/engine.py:485-500).
- note_document (575-586), when the direction came from a GSTIN rather than an assumption.
- cgst_sgst_unequal of Re 1 or less (370-377).
- lines_total_mismatch (465), when _totals passes.
- old_invoice (275-284). Replace the 365-day rule with the company's books-from date: BOOKSFROM is already fetched (connectors/tally/connector.py:105-113), so store it on Company.
Add a Company.auto_ok_codes JSON column (alembic migration in backend/alembic/versions) and edit it on web/app/(app)/companies/[id]/ledgers/page.tsx. Also require voucher.confidence (already computed in validation/engine.py:735 _confidence, but unused) to reach a per-company threshold before READY. services/vouchers.py _with_review_limit stays as the amount backstop. Show info issues muted in web/components/voucher-panel.tsx.

**Safeguard:** These stay blocking: low_confidence, weak_party_match, company_not_on_invoice, wrong_gst_type, duplicate_invoice, totals_mismatch, invalid_gstin and above_review_limit. Every code defaults to 'review' until it is deliberately moved. Policy changes are audited. Info issues stay on the posted voucher and appear in the daily digest.

**Volume:** Common: every credit/debit note, every reverse-charge bill and every bill with paisa-level rounding, plus catch-up work on old bills.

**Needs from the owner:** Which warning types the office is comfortable posting without a look (a starting list is proposed above).

### 1.2 Ship automatic posting as the default, with a trust ramp per client (S)

**Removes:** The per-entry 'Post to Tally' click for 100% of documents under the defaults, and the admin's per-company trip to switch posting rules.

**How:** Add office-level default posting rules to services/app_settings.py (always_review, auto_post, auto_create_ledgers, review_above_amount). Apply them in api/companies.py create_company (line 42 builds Company(**body.model_dump()); CompanyIn already accepts the flags at schemas/api.py:60-63), and show them on the Add company form in web/app/(app)/companies/page.tsx:103.

Add a daily 'graduation' step to pipeline/worker.py run_once, on the same kind of clock as backups:
1. Count each company's last N posted vouchers that have no 'voucher.edited' audit event (reuse cases_from_posted in extraction/evaluate.py).
2. Above the threshold, turn always_review off and auto_post on, or propose it to an admin, and record a 'company.graduated' audit event.
3. Revert automatically when a reviewer edits or rejects an auto-posted entry.

**Safeguard:** review_above_amount stays as a backstop. A daily digest lists auto-posted entries, and a random 1-in-20 sample is flagged for an after-the-fact check. Each company keeps its own kill switch. Never graduate a client before items 1.4, 1.12 and 1.16 are live.

**Volume:** Every document (100% of entries wait for a click today).

**Needs from the owner:** The owner decides three things: the amount above which entries are always reviewed, how many clean posts a client needs before auto-posting, and whether graduation switches on by itself or only proposes it to an admin. Trusting automatic posting is a professional-responsibility call per client.

### 1.3 Create new supplier and customer ledgers automatically and post the same bill (M)

**Removes:** Picking 'Create new: <party>' and clicking Post on the first bill from every new party. Today this is an error when auto-create is off and a warning when it is on, so it is never automatic either way. Also removes creating a ledger by hand in Tally when the proposed group is wrong.

**How:** validation/engine.py _party_ledger (644-665): emit new_party_ledger as 'info' when all of these hold:
- auto_create_ledgers is on;
- the GSTIN passes its checksum (normalization/gst.py is_valid_gstin);
- the name was read with confidence 0.6 or higher;
- no existing ledger has the same GSTIN or the same PAN (GSTIN characters 3-12), and gstin_conflict() is false.

Default auto_create_ledgers to True (models/company.py:28, schemas/api.py:60).

In accounting/engine.py _proposal (382-394):
- fill pincode from the address;
- set registration type 'Composition' for a bill_of_supply that carries a GSTIN;
- choose the parent group from the synced LedgerGroup tree (the sub-group the party's peers use) instead of the fixed PARTY_GROUPS.

services/posting.py post_transaction (170-180) already creates proposed ledgers before the voucher. Optional: look the GSTIN up in the public taxpayer search to fill the legal name, address, pincode and taxpayer type.

**Safeguard:** Before creating a ledger:
- Search both Sundry groups by PAN and by fuzzy name.
- Never auto-create a party without a GSTIN above an amount limit; send those to review or to a fallback ledger.

After creating:
- _check_names_free (services/posting.py) already blocks name clashes in Tally.
- Every creation is audited as 'ledger.created'.
- Auto-created ledgers are listed weekly for merging.

**Volume:** Common: the first bill from every new party, which for growing clients is most parties.

**Needs from the owner:** The amount limit above which an unregistered party (no GSTIN) is not auto-created. For the optional GSTIN lookup: a GSP or API account and its credentials, which the app does not have today.

### 1.4 Choose the right purchase or expense ledger without a person (M)

**Removes:** - Picking the purchase or expense head on the first bill from every party (weak_item_ledger).
- Re-picking it when a party bills under two heads or two rates.
- Re-picking Fixed Assets on every bill.
- Restructuring Tally groups when ledgers sit under custom sub-groups.

It also closes a silent risk: when a company has a single 'Purchase' ledger, a rent or phone bill scores 0.8 with no warning and would auto-post to Purchase.

**How:** (a) accounting/engine.py:431: treat the default as sure when exactly one ranked ledger names this invoice's rate and is not a misfit.
(b) Load LedgerGroup into AccountingContext (services/vouchers.py accounting_context, about 117-138). Resolve each ledger's primary group by walking its parents in accounting/matching.py _placed_group (214-220). Make the pickers in web/components/voucher-panel.tsx:474-483 use the resolved group.
(c) After normalize() in services/vouchers.run_extraction, add a classification call: send the line descriptions, HSN/SAC codes and the company's own ledgers under Purchase Accounts, Direct/Indirect Expenses and Fixed Assets to Claude as a JSON-schema enum (extraction/extractor.py), together with a SAC-to-head table (9972 rent, 9984 telecom, 9982 professional, 9987 repairs, 9965 freight). Record method 'ai' with its confidence. Include expense groups in rank_item_ledgers (matching.py:399) and Fixed Assets in ITEM_SIDE_GROUPS (matching.py:34-37).
(d) Make LedgerMapping (models/voucher.py:80-90) key on company, party ledger, direction, GST rate and SAC prefix, with a count. Prefer the most frequent mapping and weight reviewer picks above unconfirmed defaults. This changes _learn in services/vouchers.py:496-516.

**Safeguard:** Claude's answer must come from the enum of synced ledger names, with party, bank, capital and tax groups excluded. Accept automatically only when two of the three signals agree (AI, SAC rule, learned history); otherwise weak_item_ledger stays. Above a value limit, capital-versus-revenue bills always go to a person. The reason for each pick is stored on the voucher.

**Volume:** Common: the first bill of every party, and every bill from suppliers who bill under several heads.

**Needs from the owner:** The value above which the office always wants to decide capital versus revenue.

### 1.5 Remember which ledger a party is, and write its GSTIN back to Tally (M)

**Removes:** Confirming the same name-only party match on every bill (weak_party_match), and re-picking the ledger when the Tally ledger has no GSTIN or is spelled differently from the bill.

**How:** 1. Add a party_aliases table (company_id, GSTIN or normalized name via accounting/matching.normalize_name, ledger_name, source, created_by), with an alembic migration.
2. Write to it in services/vouchers.py _learn when a voucher posts with party method 'choice' or with a confirmed fuzzy match.
3. In accounting/matching.match_party (303-345), check it after the GSTIN lookup and before fuzzy matching, as method 'learned' with a score of 0.95, so no weak_party_match is raised.
4. After a successful post, send a ledger alteration (a new xml_builder function using ACTION='Alter'). It adds the GSTIN through LEDGSTREGDETAILS.LIST when the ledger has none, and adds the printed name as an alias through NAME.LIST.
5. Accept a strong fuzzy match (similarity 92 or more) automatically when the ledger's GSTIN is blank, the state matches and no second candidate is close.

**Safeguard:** Never learn or alter a ledger when gstin_conflict() is true, and require a PAN match when both sides have a GSTIN. Aliases are visible, editable and deletable on the Ledgers tab, and a mapping is dropped as soon as a reviewer overrides it. Tally is altered only after the voucher has posted.

**Volume:** Common in older Tally books, where many ledgers have no GSTIN or a different spelling. Without this it repeats on every bill from those parties.

**Needs from the owner:** Access to a real TallyPrime to confirm the ledger alteration XML.

### 1.6 The app creates missing tax, round-off and purchase/sales ledgers itself (M)

**Removes:** Going into TallyPrime to create Input/Output CGST/SGST/IGST/Cess ledgers (including rate-wise ones), RCM payable, Round Off and Purchase/Sales ledgers, then coming back to sync. Also removes renaming correctly set-up tax ledgers just because their names lack the word 'cgst', 'sgst' or 'igst'.

**How:** Extend ProposedLedger (schemas/canonical.py:41-52) with a kind (party, item, gst_duty, rcm_payable, round_off) and tax fields (duty head, rate, GST applicable, type of supply). Emit TAXTYPE=GST, GSTDUTYHEAD, RATEOFTAXCALCULATION and GSTAPPLICABLE in connectors/tally/xml_builder.create_ledgers_request (130-176).

In accounting/engine.py, three resolvers return a LedgerRef with proposed= instead of a blocking problem: _resolve_tax_ledgers (454-531), _resolve_round_off (534-551) and _resolve_item (433-450). Name each proposal after the company's existing pattern; other_rates already lists the sibling ledger names.

Add the tax-type, duty-head, rate, GST-applicable and type-of-supply fields to LEDGER_FETCH (xml_builder.py:33-45) and to the Ledger model. Then matching.find_tax_ledger (456-482) can match by Tally properties first and fall back to name words. posting.post_transaction already creates every proposed ledger.

**Safeguard:** - Before proposing, match on duty head and rate as well as name, so GST balances are not split across duplicate ledgers.
- Create only rates in VALID_GST_RATES, only under standard groups.
- Never create one for a bill whose rate cannot be worked out.
- Audit each creation and mark it 'created by the app'.
- Test against devtools/mock_tally and a real TallyPrime before switching it on.

**Volume:** Occasional per document, but it blocks every affected entry: every client onboarding, the first bill at a new GST rate and the first RCM bill.

**Needs from the owner:** The office's naming convention for tax ledgers (for example 'Input CGST @ 9%'), and a real TallyPrime to verify the GST duty XML.

### 1.7 Sync ledgers from Tally automatically (S)

**Removes:** Clicking 'Sync ledgers from Tally' after adding a company and whenever staff change ledgers in Tally. Also removes fixing entries stuck on stale 'missing ledger' or 'unknown ledger' errors and name clashes, and re-picking ledgers after a rename in Tally.

**How:** - In pipeline/worker.py run_once, about every 15 minutes (next to AUTO_POST_EVERY_SECONDS) and before each auto_post_ready round: call services/ledger_sync.sync_masters(..., actor_id=None) for each company whose books are open (reuse vouchers._open_books), then vouchers.reevaluate_open.
- Also call it at the end of api/companies.create_company.
- In services/posting.py _check_names_free (73-83): use the live fetch_ledgers result to refresh the cache, re-evaluate, and retry once if the clashing ledger has the same GSTIN.
- In ledger_sync.py:27-48, match existing rows by external_id (the Tally GUID, models/ledger.py:25) before name. On a rename, update the Ledger row, LedgerMapping.party_ledger and item_ledger, and the ledger names in open vouchers' choices.

**Safeguard:** sync_masters deletes every cached ledger missing from Tally's answer (ledger_sync.py:46-48). So skip the replace when Tally returns 0 ledgers, when the count drops sharply, or when the company is not open. Skip a company while one of its vouchers is POSTING. Audit every automatic sync as a system action.

**Volume:** Common: every client onboarding and every ledger change made directly in Tally.

### 1.8 Posting heals itself: wait for Tally, retry, and look in Tally instead of asking a person (M)

**Removes:** - Opening and re-posting each post_failed entry after Tally was closed, busy or on another company.
- Searching the Day Book after an interrupted or timed-out post.
- Mass post_failed entries from 'Post N ready' while Tally is down.
- Mass post_failed entries from Educational-mode or books-period date rejections.

**How:** Add find_voucher(company, remote_id) to the AccountingConnector protocol (connectors/base.py) and to connectors/tally/connector.py. Build it on xml_builder.export_collection_request (72) as a Voucher collection filtered on REMOTEID, falling back to REFERENCE + DATE + PARTYLEDGERNAME.

Add a WAITING_FOR_TALLY status, or READY with a next_post_at, in models/voucher.py.

In services/vouchers.post_voucher (576-589), sort failures into three kinds:
- Tally unreachable, company not open, or a date refusal matching _DATE_HINT: go back to waiting with backoff instead of POST_FAILED.
- 'Ledger does not exist' or a name clash: sync (item 1.7), re-evaluate and retry once.
- Genuine data rejections: these alone stay POST_FAILED.

requeue_stale (349-366), _mark_interrupted (612-633) and the timeout path call find_voucher. If the voucher is found, mark it POSTED with its external_id; if not, return it to READY.

In api/vouchers.py post_ready (86-113), check _open_books first, return 409 if Tally or the company is unavailable, and stop at the first unreachable result.

Store books_from at sync and raise an issue for invoice dates before it. In _auto_post_company (680-683), pause the round after two consecutive date rejections and set a wait reason on the company.

**Safeguard:** Mark an entry POSTED only on a single exact REMOTEID match; an ambiguous or failed lookup leaves it with a person. already_posted() and REMOTEID still guard against re-sending. Cap retries and alert after repeated failures. README.md:115 says the connector is verified only against the mock, so verify the REMOTEID filter and resend behaviour on a real TallyPrime first.

**Volume:** Occasional per entry, but when it happens it hits a whole batch.

**Needs from the owner:** A real TallyPrime installation to verify the REMOTEID lookup and the resend behaviour.

### 1.9 AI outages wait and retry instead of turning into typing (S)

**Removes:** - Typing documents by hand, or clicking 'Read again with AI' one at a time, after a rate limit, an overload, an internet drop or exhausted Anthropic credit.
- Restarting the app twice to unstick items left in 'Reading' or 'AI is reading'.
- Clicking Retry on files that failed for a passing reason.

**How:** Already done: requeue_waiting_for_ai (services/vouchers.py:752, called from api/settings.py:133) re-queues documents once a missing or rejected key is fixed.

Still to do:
1. Add Voucher.next_attempt_at (alembic) and exponential backoff in run_extraction (services/vouchers.py:301-304): 1, 5 and 30 minutes, then 2 hours, up to 24 hours, honouring retry-after. process_pending (370-377) claims only vouchers that are due. Retryable failures no longer count toward MAX_EXTRACTION_ATTEMPTS.
2. Pause the whole queue (a circuit breaker) on connection errors.
3. Extend extraction/extractor.waiting_for_ai (186-192), which today matches only 'not set up' and 'rejected', to cover 'credit balance too low' 400s and unknown-model errors. Those documents then wait instead of going manual, and are re-queued after the next successful read.
4. Call both requeue_stale() functions from run_once every 5 minutes, not only at startup (pipeline/worker.py:169), and refresh claimed_at as a heartbeat during long reads.
5. Retry retryable parse errors with backoff (worker.py:105-108).
6. Add a bulk 'Read all again' per company and a status-bar banner: 'AI unavailable: N documents waiting'.

**Safeguard:** Never re-queue a voucher a person has edited or posted; requeue_waiting_for_ai already checks the voucher.edited audit. Cap retries and daily spend (cost_usd is already tracked). Genuinely unreadable files and refusals stay manual. Stale thresholds stay longer than the slowest real call (180 s for AI, 30 s for Tally).

**Volume:** Occasional events, but each one hits every queued document.

**Needs from the owner:** A daily AI spend ceiling.

### 1.10 One exceptions list across all clients, with alerts and 'waiting for Tally' shown (M)

**Removes:** - Clicking through every client's Documents tab to find work.
- Entries that wait silently because a client's books are not open in Tally.
- Older needs-review and failed items hidden behind the 200-document cap.
- Learning about failed backups or a dead AI key only by chance.

**How:** 1. Add GET /api/attention (new file api/attention.py). It groups vouchers by company and status: needs_review with the top issue code, post_failed, waiting_for_tally with its reason, and waiting_for_ai. It also reports failed documents and backup age.
2. Show counts as badges in web/components/sidebar.tsx (111-124) and on the companies table, and add a cross-company 'My queue' page.
3. Store a per-company wait reason when auto_post_ready skips a company (services/vouchers.py:712-714). Show it in web/components/status-bar.tsx using each company's own connector_url, for example 'Open ABC Traders in Tally: 12 entries waiting'.
4. Filter on the server: join voucher status in api/documents.py list_documents (93-100) with paging and true counts, or reuse GET /companies/{id}/vouchers?status= (api/vouchers.py:45). Wire this through web/lib/api.ts and the filter tabs in companies/[id]/page.tsx.
5. Add SMTP or webhook (WhatsApp/Telegram) settings in services/app_settings.py. A worker job sends a daily digest plus immediate alerts: backup failed, AI key rejected or out of credit, Tally unreachable for more than X hours.

**Safeguard:** Digests carry counts and links only, never amounts or GSTINs. Alerts are rate-limited. Roles are respected: preparers see only their own uploads.

**Volume:** Every reviewer session, every day.

**Needs from the owner:** An SMTP account or a webhook address for alerts, and who should receive the digest.

### 1.11 The app starts itself and keeps running (S)

**Removes:** Double-clicking start.bat every morning and after every restart or crash, and noticing that its window was closed.

**How:** Add 'start.ps1 -Install', which registers a Windows Scheduled Task (at startup or logon, hidden window, restart on failure). Wrapping uvicorn and 'next start' as services with WinSW or NSSM is an alternative.

Replace the wait-then-exit loop (start.ps1:96-99) with a supervisor that restarts whichever child process exited, with a backoff, and logs each restart to backend/data/logs/app.log and the audit log. Run app.devtools.upgrade only on the first start, not on every restart.

**Safeguard:** A lock file or port check stops two copies fighting over ports 8000 and 3000 or the SQLite file. Restarts are capped per hour so a crash loop stops. The task runs as the office user so Tally on 127.0.0.1 stays reachable.

**Volume:** Daily, and after every Windows update.

**Needs from the owner:** Which Windows user account the app should run under.

### 1.12 Safety flags that must stop an auto-post (several bills in one file, missing pages, foreign currency, cancelled) (S)

**Removes:** A person paging through every file because the AI's warnings sit in hidden notes. This is what makes it safe to turn review off.

**How:** Add structured fields to InvoiceExtraction (schemas/extraction.py:72-98) and NormalizedInvoice (schemas/invoice.py):
- documents_in_file
- pages_not_seen
- amount_in_words_matches
- currency (ISO code)
- is_cancelled, with the quoted stamp text
- tds_or_advance_deducted

Tell the prompt to fill them (extraction/prompt.py:101-103, 142-144, 158). Make extractor._left_out_notice (extractor.py:293-302) fill pages_not_seen, including pages past max_pages=60 (config.py:33).

Add issues in validation/engine.py _find_issues (744-775):
- several_documents: error, until Phase 2 splits files automatically.
- pages_not_seen: error.
- non_inr_currency: error.
- cancelled_invoice: error; for sales, offer to post it as a cancelled voucher so the number series stays complete.
- amount_in_words_mismatch: warning.

Render invoice.notes in web/components/voucher-panel.tsx; today notes appear only as a type field in web/lib/api.ts:289.

**Safeguard:** These are errors and never info, so nothing that raises them can auto-post.

**Volume:** Protects every auto-posted document; the flags themselves fire occasionally.

### 1.13 Duplicates and documents that should not be posted are set aside automatically (S)

**Removes:** - Clicking Reject on exact re-sent copies, proformas, quotations, purchase orders and challans.
- The deadlock where two open copies block each other and the surviving copy stays stuck after the other is rejected.
- Near-duplicates with a differently written invoice number being posted twice.
- Re-uploads parked as duplicates when the original had failed.

**How:** Add an auto_triage step after evaluate() in services/vouchers.run_extraction:
- If _duplicate_lookup finds a posted or open voucher that matches on party GSTIN (or matched ledger), invoice number, financial year, date and grand total within Re 1, reject the new one as a system action with the reason 'duplicate of #N'.
- Proformas, quotations, POs and challans read with high confidence (validation/engine.py:86-95, 147-155) go to REJECTED with the reason 'not for posting' and appear under a 'Set aside' filter.

After any reject or post, re-evaluate the other open vouchers with the same invoice number (in reject_voucher, 719-732).

In _duplicate_lookup:
- Normalise invoice numbers (line 170): strip separators, spaces and leading zeros, and fold the different financial-year spellings together.
- Compare parties by matched ledger or matching.normalize_name (line 190).
- Add a 'possible_duplicate' warning for the same party with a total within Re 1 and a date within a few days.

In services/documents.ingest_upload (38-63):
- If the original document FAILED, re-queue it.
- If its voucher was REJECTED, say so and offer Reopen.
- If the stored file is missing, restore it.

**Safeguard:** Auto-reject only on an exact match of every key field. A different amount or date stays in review, because it may be a revised invoice. Everything set aside is listed and can be reopened in one click (reopen_voucher exists). These are audited as system actions. possible_duplicate is only a warning, because monthly rent legitimately repeats.

**Volume:** Occasional; very common at go-live and when clients send both WhatsApp and email copies.

### 1.14 Stop asking people to confirm what the app can already prove (M)

**Removes:** - low_confidence reviews where the totals reconcile to the paisa and the GSTIN matches a Tally ledger exactly.
- 'Hard to read' warnings on GSTINs that simply are not printed (B2C, unregistered).
- Warnings and blocks on the company's own name or GSTIN.
- Setting 'Entry for your books' by hand when the company is printed under a branch GSTIN or trade name.

**How:** (a) Before _low_confidence (validation/engine.py:721-732), add a corroboration pass:
- Amounts count as verified when _totals passes within Re 1, the lines sum correctly and _tax_rate passes.
- A GSTIN counts as verified when it passes the checksum and equals a Tally ledger's GSTIN (matching.py:322-332) or company.gstin.
- The party name counts as verified when the party matched by GSTIN.
Raise those fields' confidence to 0.99 and audit what cleared each flag.

(b) In extraction/prompt.py (43, 167) and normalize.py _Reader.gstin (100): a GSTIN that is clearly not printed, or printed as URP/NA/Unregistered, becomes null with high confidence.

(c) Expose _company_role (accounting/engine.py:216-231) on AccountingResult, and skip low_confidence and invalid_gstin on the company's own side.

(d) Add a company aliases table (trade names, all GSTINs, partner names) and use it in _company_role; also compare Company.name. Infer the direction when the counterparty matches by GSTIN in exactly one of Sundry Creditors or Sundry Debtors, generalising _is_supplier (engine.py:244-249). Learn the direction per counterparty from posted vouchers.

**Safeguard:** Each check must be independent of the value in doubt: totals that add up do not prove the date. A checksum alone is not enough; it also needs a ledger, company or text-layer match. Keep the absent-GSTIN warning for B2B purchases above a limit, and for parties whose Tally ledger has a GSTIN, because input credit may be lost. Conflicting direction evidence keeps the entry in review.

**Volume:** Common: every B2C or unregistered bill, every phone photo, and every client that trades under a branch or trade name.

**Needs from the owner:** Each client's trade names and branch GSTINs, if they are not in Tally.

### 1.15 Counter and cash sales without a customer name post to a default ledger (S)

**Removes:** Picking a party ledger on every walk-in sale below Rs 50,000 (missing_party_name).

**How:** Add a company setting default_b2c_ledger: 'Cash' under Cash-in-Hand, or a 'Walk-in Customers' debtor created automatically if missing. Use it in accounting/engine.py _resolve_party (352-362) when the direction is sales, the buyer has no GSTIN and no name, and the amount is below UNREGISTERED_NAME_LIMIT (validation/engine.py:161-169). Take the place of supply from the company's state.

**Safeguard:** Only for sales below the Rule 46 limit, never for purchases. The invoice-number duplicate check still applies.

**Volume:** Common for retail clients: every counter bill.

**Needs from the owner:** Which ledger each retail client uses for walk-in sales.

### 1.16 Correct GST treatment without a reviewer: unregistered and composition clients, and credit that cannot be claimed (M)

**Removes:** Zeroing the tax fields by hand on every GST purchase of an unregistered or composition client, or keying those bills in Tally. Also removes catching bills whose input credit cannot be claimed, which today raise no warning or a misleading one: no company GSTIN on the bill, someone else's GSTIN, or past the Section 16(4) deadline.

**How:** 1. Add Company.gst_status (regular / composition / unregistered). Read it from Tally's company GST details at sync (extend COMPANY_FETCH, xml_builder.py:32), or default to unregistered when there is no GSTIN; an admin confirms it once.
2. In accounting/engine.py _postings (678-695) and _resolve_tax_ledgers (454-502), add the tax to the item or expense entry instead of the Input ledgers when:
   - the status is not regular;
   - on a purchase, the buyer GSTIN is confidently absent, or valid and different from the company's;
   - the Section 16(4) cut-off (30 November after the financial year) has passed.
3. Raise 'itc_not_claimable' with the reason, and keep the GST figures in the narration.
4. Close the name-only fallback in _company_role (line 229), so a different valid buyer GSTIN is never silently accepted.

**Safeguard:** Apply this automatically only when the buyer GSTIN box was read as empty with high confidence, or holds a checksum-valid different GSTIN; low-confidence reads go to review. Allow a per-company override and audit every reclassification. An admin confirms each client's status once, because a wrong 'unregistered' setting would drop credit for a regular dealer.

**Volume:** Common: every GST purchase of non-regular clients, and many restaurant, hotel and retail bills.

**Needs from the owner:** Confirm each client's GST status (regular, composition or unregistered) once.

## Phase 2 - Documents arrive by themselves

Nobody uploads or sorts files. Emails, scanner output and WhatsApp forwards are picked up, routed to the right client by GSTIN, unpacked or converted, and split into one entry per bill. With Phase 1, a clean invoice from a known party then needs zero clicks.

### 2.1 Watched folders, an office mailbox and an upload API (L)

**Removes:** Opening the app, finding the client and dragging files in for every batch; the 50-file limit; picking files out of folders one by one.

**How:** Add a new package, backend/app/intake/. Its sources are polled from pipeline/worker.py run_once on their own clock, and each calls services/documents.ingest_upload with uploaded_by=None (already nullable, models/document.py:37). The channel and sender go into the 'document.uploaded' audit event.

(1) A watched folder, data/inbox/<client> or data/inbox/_office (a OneDrive or Drive sync folder works). Ingested files move to processed/ and rejects to rejected/<reason>/.
(2) An IMAP poller (stdlib imaplib) on an office mailbox, mapping plus-addresses (bills+<client>@) or sender addresses to a client.
(3) Per-client upload-only API tokens (hashed and revocable), accepted in app/deps.py alongside the session cookie, for scanners, WhatsApp Business webhooks and phone shortcuts.
(4) Folder upload and folder drop in web/app/(app)/companies/[id]/page.tsx: webkitdirectory on the input (281) and webkitGetAsEntry on drop (245).

**Safeguard:** - Accept mail only from allowlisted senders or domains per client; quarantine the rest.
- Apply the same size and type checks as browser upload.
- The sha256 dedupe already stops a file being ingested twice.
- Move or flag each source item after ingest, so a crash cannot re-ingest it.
- Tokens can upload only; they cannot post to Tally.

**Volume:** Every document.

**Needs from the owner:** An office mailbox (address, IMAP host and an app password) and the folder paths to watch. For WhatsApp, a WhatsApp Business API account.

### 2.2 Route each document to the right client by GSTIN, and move misfiled ones (L)

**Removes:** Working out which client a bill belongs to before uploading; sorting mixed batches; uploading an invoice between two clients twice; deleting and re-uploading a bill filed under the wrong client.

**How:** - Allow Document.company_id to be empty (an 'Unsorted' state), or use a holding company.
- Extract with company-neutral instructions. extraction/prompt.py already tells the model to decide seller and buyer from the document itself.
- Match checksum-valid seller and buyer GSTINs against a new company_gstins table that holds every registration of every client. Fall back to PAN, then to accounting/matching name_similarity.
- Move the document and re-extract with the client's context. When both parties are clients, create a linked copy for the second one.
- Add POST /documents/{id}/move to api/documents.py. It re-points Document and Voucher company_id (stored files are content-addressed and stay put), resets the voucher to PENDING and runs the target client's sha256 dedupe.
- Move automatically when company_not_on_invoice fires and the counterparty's GSTIN equals another client's.
- Make Company.gstin required and unique (models/company.py:20).

**Safeguard:** Route automatically only on one exact, high-confidence GSTIN match. Zero matches, several matches or a name-only match stay in Unsorted for a person. Refuse to move a POSTING or POSTED voucher. Audit the move in both companies. company_not_on_invoice stays as a backstop.

**Volume:** Every upload batch; every document once channels from 2.1 feed a shared inbox.

**Needs from the owner:** Every client's GSTINs, including branch registrations.

### 2.3 Split files holding several bills, and join photos of one bill (L)

**Removes:** Splitting scanner-batch PDFs by hand, and the silent loss of every bill after the first. Also removes rejecting the page-2 photo of a bill and typing its totals into the page-1 entry.

**How:** Before extraction, run a segmentation pass: a cheap Claude call that returns page ranges, document types and which pages are Original/Duplicate copies of the same invoice.

In pipeline/worker.py, create child Documents with a parent_document_id using pymupdf insert_pdf(from_page, to_page) (parsing/pdf.py). Each child gets its own voucher, so the one-voucher-per-document rule (models/voucher.py:40) still holds.

Merge photos from the same upload batch when one has a header but no total and the next has totals but no header; Pillow save_all turns them into one PDF. Also offer a 'these photos are one bill' option in the upload screen.

This replaces the interim several_documents error from item 1.12.

**Safeguard:** Every page must belong to a child or be labelled an annexure. Original/Duplicate/Triplicate copies collapse into one by invoice number, GSTIN and total. Each child runs the normal duplicate check. The parent file stays as the record. If segmentation is unsure, the file stays whole with an error.

**Volume:** Common: scanner batches, phone-scanner bundles and multi-photo WhatsApp bills.

### 2.4 Unpack and convert any common file at intake (ZIP, HEIC, TIFF, email, .doc/.xls, locked PDFs) (M)

**Removes:** Unzipping archives, converting HEIC or TIFF, re-saving .doc and .xls files, removing PDF passwords, converting Word files that have no text, and uploading everything again.

**How:** In services/documents.ingest_upload and ingestion/filetypes.detect (70-90):
- Expand ZIP members into child documents.
- Convert HEIC with pillow-heif, and multi-frame TIFF into a PDF with Pillow.
- Take attachments out of .eml (stdlib email) and .msg (extract-msg) files.
- Convert .doc and .xls with LibreOffice headless (or read .xls with xlrd).

In parsing/pdf.py (18-21):
- Try stored per-client passwords (encrypted with app/security_box.encrypt) and the usual PAN and date-of-birth patterns through doc.authenticate().
- Save an unlocked working copy for reading.
- Add an 'Enter password' field on the failed document that saves the password for that client and retries automatically.

In parsing/office.py parse_docx: also read section headers, footers and text boxes, or convert the DOCX to PDF.

**Safeguard:** - Zip-bomb limits on member count, total size and nesting depth; encrypted archives are rejected with a clear message.
- Every extracted member goes through detect() and the sha256 dedupe.
- Original files are always kept.
- Passwords are never logged, and attempts per file are capped.

**Volume:** Occasional: ZIPs from clients, locked utility and telecom bills, legacy Office files.

**Needs from the owner:** LibreOffice installed on the office PC for .doc and .xls files. A client's document password the first time a new one appears that no pattern can derive.

### 2.5 Read very large and very long documents without splitting them (M)

**Removes:** Splitting or compressing big scans and long invoices, and typing an entry by hand after a 'cut off' or 'too large' message.

**How:** In extraction/extractor.py:
- If the answer is still cut off after the 32k retry (128-140), retry in summary mode: no line items, just per-rate tax summary rows and totals. That is all the engine needs (accounting/engine.py:685).
- On RequestTooLarge or a page-count BadRequest (356-361), retry with JPEG page images at a lower quality.
- Read long PDFs in chunks (header page, item windows, last page) and merge the results.

Always render the true last page, even past max_pages (parsing/pdf.py:29-33), and pass the real page count to _left_out_notice. At intake, re-encode oversized scans instead of rejecting them over 25 MB (config.py:32), keeping the original.

**Safeguard:** Merged line items must agree with the printed taxable value (lines_total_mismatch). Any page left unread sets pages_not_seen (item 1.12), which blocks auto-post.

**Volume:** Rare to occasional: distributor and pharma invoices, bulk scans.

## Phase 3 - Fewer invoices need a person

Turn the common reading and accounting exceptions into automatic fixes, and make the rest quick to resolve. This phase comes before the new document types because each item is small or medium and affects invoices every day. If bank-statement keying dominates the office's workload, start item 4.2 in parallel.

### 3.1 Repair obvious misreads automatically (GSTIN look-alikes, swapped dates, totals that can be derived) (S)

**Removes:** - Retyping GSTINs with 0/O, 1/I, 5/S, 8/B or 2/Z misreads, and fixing same-GSTIN-on-both-sides.
- Checking day/month swaps.
- Typing a missing total, taxable value or party name that the app could work out.

**How:** - GSTIN: add repair_gstin(raw, candidates) to normalization/gst.py, using gstin_check_char (120) and the GSTIN shape rules. Accept the single variant that also matches a Tally ledger GSTIN, the company GSTIN, a text-layer GSTIN or the e-invoice QR.
- Same GSTIN on both sides, equal to the company's: blank the counterparty's GSTIN and match by name.
- Dates: when a date is low-confidence or in the future (validation/engine.py:264-273), try the day/month swap. Accept it when exactly one candidate falls in the open period and fits the party's invoice-number sequence.
- Totals: derive grand_total = taxable + tax + round-off, or taxable = total - tax, at derived confidence (normalize.py:205).
- Party name: fill it from the ledger matched by GSTIN, and skip missing_party_name (validation/engine.py:216).
- Optional per-company setting: small expense bills with no number get a reference such as DOC-<id>.

**Safeguard:** Accept a fix only when exactly one candidate passes every independent check. Record the original reading and the reason in the audit trail, and lower that field's confidence. Never check a derived value against itself: skip the totals check and keep a warning. Generated numbers are allowed only for small expense purchases, never for sales or bills carrying input credit.

**Volume:** Occasional on digital PDFs, common on scans and phone photos.

**Needs from the owner:** Whether to allow generated references for small petty bills, and the amount limit.

### 3.2 Verify readings against the e-invoice QR and the PDF's own text (M)

**Removes:** Checking the invoice number, GSTINs, date and total by eye on digital PDFs and e-invoices, and reviews of long or unusual invoice numbers that are printed exactly as read.

**How:** - Decode QR codes from the rendered page images (parsing/pdf.py, parsing/image.py) with zxing-cpp.
- Decode the IRN payload with pyjwt, already a dependency (pyproject.toml:15), and store it on Document.parsed.
- In a reconcile step after normalize(), set the invoice number, date, GSTINs, document type and total to confidence 1.0 from the QR. Raise qr_mismatch when the AI's reading disagrees. Store the IRN on the voucher for duplicate checks.
- For PDFs with a text layer (doc.text, stored at worker.py:85), confirm GSTINs, the invoice number and amounts appear verbatim.
- Accept invoice_number_format (validation/engine.py:49, 240) when the number appears verbatim.
- Return source_text and page in VoucherOut (schemas/api.py:198) and highlight them in the document viewer.

**Safeguard:** Trust only a well-formed IRN payload, ideally signature-checked against the IRP certificate, and ignore UPI payment QR codes. The text layer may only confirm a value or choose between candidates, never invent one, and matches must be unique.

**Volume:** Most digital invoices; e-invoices from mid-sized and large suppliers.

### 3.3 A second, targeted AI read before anything goes to a person (M)

**Removes:** - Hand-correcting totals_mismatch, mixed_gst, cgst_sgst_unequal and future_date errors, missing fields and low-confidence values from poor scans.
- A 'Read again' click that just repeats the same request.

**How:** In services/vouchers.run_extraction, after evaluate(), when a re-readable issue appears:
1. Send one follow-up with the page images, the first answer and the exact failing checks (for example 'taxable + tax does not equal total: re-read taxable, CGST, SGST and total; are there TCS or other charges?'). Use a higher effort setting or the other model. Add build_followup to extraction/prompt.py.
2. Merge only the re-read fields, and keep the result only if every check now passes or both readings agree.
3. For low-confidence fields, send a full-resolution crop around the field's source_text and page.

Other changes:
- Preprocess scans and photos (deskew, contrast, 200-300 DPI renders) in parsing/image.py and parsing/pdf.py.
- Treat an answer that fails the schema as retryable once.
- If a file is still unreadable, automatically ask the uploader or client for a clearer copy, through the alerts from 1.10 or the mailbox from 2.1.

**Safeguard:** At most one or two follow-ups per document, with a cost ceiling. Never accept a changed grand total unless the arithmetic then reconciles. Keep both readings in voucher.extraction and the audit trail, and mark AI-corrected fields so spot checks can find them.

**Volume:** Common: every invoice with a blocking read error.

**Needs from the owner:** A per-document AI cost ceiling.

### 3.4 Learn each supplier's habits from corrections (M)

**Removes:** Fixing the same supplier's recurring misreads every month.

**How:** Build a supplier profile table from posted vouchers: GSTIN, canonical name, invoice-number pattern and last number, usual GST rates, usual amount range, state and RCM flag. Pass the matched supplier's profile in the per-request user text of the extraction call; build_instructions (extraction/prompt.py:187) stays unchanged so prompt caching keeps working. Use the profile in the reconcile step to support the number format and GSTIN.

On every post, run an extraction/evaluate.py-style comparison of the AI's reading against the final values, and show accuracy per field and per supplier. This also feeds the trust ramp in item 1.2.

**Safeguard:** A profile only supports a value; it never overrides what is printed. If a supplier changes GSTIN or numbering, raise a warning instead of silently 'correcting' it. Build profiles only from posted vouchers.

**Volume:** Common: repeat suppliers make up most of the volume.

### 3.5 Split mixed-rate bills and charge lines across ledgers (M)

**Removes:** Keying mixed-rate invoices in Tally by hand (for companies with rate-wise tax ledgers), re-posting freight, packing and discount lines into their own ledgers, and wrong tax-ledger choices caused by misread line rates.

**How:** In accounting/engine.py _gst_rate (639-653) and _postings (678-695):
1. Group lines by GST rate and ledger.
2. For each group, post the taxable value to that rate's item ledger and each tax head to find_tax_ledger at that rate.
3. Reconcile per-rate tax to the printed totals, putting any rounding on the largest group.

LedgerChoices (accounting/types.py:36-42) gains per-rate overrides.

Add a line kind to the extraction (item, freight, packing, insurance, discount), and map each kind to a ledger by company setting, learned mapping or the AI pick from item 1.4. Carry ExtractedLineItem.discount into InvoiceLine.

When line rates disagree with the header (tax_rate_mismatch, invalid_gst_rate), derive the rate as header tax divided by taxable value, snapped to a standard rate.

**Safeguard:** Split only when the lines add up to the taxable value and the per-rate tax matches within tolerance. Reclassify a charge line only when it is explicitly labelled as such. Anything else goes to review.

**Volume:** Common: mixed-rate invoices in many trades, and freight or packing lines on goods bills.

**Needs from the owner:** Which charge ledgers each client keeps (Freight Inward, Packing, Discount).

### 3.6 Represent TCS, extra charges and the amount in words (M)

**Removes:** Reworking the amounts, or keying the bill in Tally, when a bill adds TCS, reimbursements, insurance or post-tax charges or discounts (a blocking totals_mismatch today).

**How:** Add these fields to schemas/extraction.py ExtractedTotals (62-69) and to schemas/invoice.py:
- tcs
- other_charges (label, amount, taxable flag)
- post_tax_discount
- amount_in_words, parsed by a words-to-number helper in normalization/values.py
- tax_summary rows

Include them in accounting/engine.py _amounts (597-609) and validation _totals (475-501). Post TCS to TCS Receivable/Payable and other charges to mapped ledgers, auto-created through item 1.6. Use the amount in words and the tax summary as second readings of the total and the per-rate tax. Add the fields to the review screen.

**Safeguard:** Adopt a corrected value only when every independent reading agrees. The charge ledgers must exist or be proposed under the right group. Otherwise the mismatch stays an error.

**Volume:** Occasional.

### 3.7 Adjust credit and debit notes against the original bill (M)

**Removes:** Changing each note's bill allocation in Tally from 'New Ref' to 'Agst Ref', and knocking off bills by hand so that ageing reports are right.

**How:** - Add original_invoice_number and original_invoice_date to the extraction schema and NormalizedInvoice.
- Give Entry (schemas/canonical.py:79-84) a bill_type (New Ref / Agst Ref / Advance / On Account), written by xml_builder._ledger_entry (186-191).
- Find the original among posted vouchers, or among Tally's outstanding bills through a new connector.fetch_outstanding built on export_collection_request.
- Check that the note does not exceed the pending amount.
- Use the 'issued by the buyer' wording the prompt already asks for to settle a debit note's direction.

**Safeguard:** Use Agst Ref only when the original bill is found exactly, for the same party, with enough pending balance. Otherwise fall back to New Ref or On Account with a warning.

**Volume:** Occasional: every credit or debit note.

### 3.8 Make the remaining reviews fast: full edit controls, next-entry flow, bulk actions (M)

**Removes:** - Dead ends: there are no controls for is_invoice, reverse charge, place of supply, line items or tax ledgers, so the person rejects the entry and keys it in Tally.
- Extra navigation between entries.
- Posting entries with the same harmless warning one at a time.
- Retyping corrections that 'Read again with AI' threw away.

**How:** In web/components/voucher-panel.tsx (247-383), add:
- an is_invoice toggle, and derive is_invoice from document_type in normalize.refresh_derived (258-276);
- a reverse-charge toggle and a place-of-supply select;
- an editable line-items table;
- a tax-ledger override (extend LedgerChoices).

Also:
- Add a preview endpoint that runs evaluate() without saving.
- Add 'Post and open next' to documents/[docId]/page.tsx, an issue-code column in the document list, and bulk post/reject/read-again on selected entries.
- When a new reading arrives (request_extraction at services/vouchers.py:793-807, run_extraction at 324), keep the fields a person edited (found from the voucher.edited audit or confidence 1.0), or show old and new values side by side.

**Safeguard:** Bulk post goes through post_voucher, which re-validates, so errors still block. Edits are audited and re-validated. A derived is_invoice must never let a proforma or receipt post.

**Volume:** Every remaining review.

### 3.9 Reverse charge and blocked input credit decided by rules (M)

**Removes:** Passing reverse-charge (RCM) entries by hand for GTA, advocate, director-fee, sponsorship and imported-service bills that print nothing. Also removes reversing blocked credits under Section 17(5) later.

**How:** - Add rules by SAC/HSN and supplier type (unregistered, foreign, GTA 9965, legal 9982). They set reverse_charge and compute the tax at the notified rate, instead of skipping zero-tax amounts (accounting/engine.py:170, 467-468), through the existing RCM path.
- Keep an 'input credit eligible' flag per expense ledger and per HSN/SAC (for example 9963 restaurant services, motor vehicles); when ineligible, move the tax into cost.
- The reverse-charge toggle comes from item 3.8.

**Safeguard:** Infer RCM only when the bill charges no GST and the supplier is unregistered or the SAC is clearly notified; otherwise raise a warning. A person approves each new ineligibility rule. A monthly report lists the reclassified amounts.

**Volume:** Occasional.

**Needs from the owner:** The CA confirms the office's RCM and blocked-credit rule list once, plus client-specific exceptions such as a GTA that opted for forward charge.

### 3.10 Late bills dated in the open GST period (S)

**Removes:** Changing voucher dates in Tally for bills that arrive after that month's GSTR-3B was filed.

**How:** Add Company.returns_filed_upto. In accounting/engine.py _transaction (734-744), when a purchase's invoice date falls in a filed period, date the voucher on the first open day (or the upload date) and keep the invoice date as reference_date. xml_builder.py:223-224 already sends DATE and EFFECTIVEDATE; add REFERENCEDATE. Show both dates in voucher-panel.tsx (260-270).

**Safeguard:** Never move a bill across a financial year automatically, and apply this to purchases only. The original date goes into the narration.

**Volume:** Common: every late-arriving purchase bill.

**Needs from the owner:** Who updates 'returns filed up to' each month. This could be set automatically when the office marks a filing as done.

### 3.11 Check duplicates against what is already in Tally (L)

**Removes:** Searching Tally's Day Book before posting old or catch-up bills; double entries when a colleague keyed a bill directly into Tally; entries deleted or altered in Tally still showing as 'In Tally'.

**How:** Add connector.fetch_vouchers(company, from, to) to connectors/base.py and tally/connector.py through export_collection_request. It fetches DATE, VOUCHERTYPENAME, REFERENCE, VOUCHERNUMBER, PARTYLEDGERNAME, PARTYGSTIN, AMOUNT, REMOTEID, GUID and ALTERID.

The worker caches these incrementally per open company in a new tally_vouchers table, and _duplicate_lookup in services/vouchers.py (157-196) checks them as 'already in Tally'. Posted entries whose REMOTEID vanished or whose ALTERID changed are flagged. With this in place, old_invoice can become info.

**Safeguard:** Read-only. Limit it to the current and previous financial year and fetch incrementally by ALTERID so large books don't stall Tally. A match becomes a review issue, never a silent drop.

**Volume:** Occasional, but very high at go-live and in offices where staff still key urgent bills straight into Tally.

## Phase 4 - Cover the document types still keyed by hand

Bring the largest remaining manual work into the same read, check and post pipeline: bank statements, receipts and payments, registers, TDS, GSTR-2B and stock items. Items are ordered by office volume, after a shared foundation.

### 4.1 Foundation: classify each document and let one document produce many entries (M)

**Removes:** Rejecting bank statements, receipts and challans as 'not an invoice', and an upload screen that invites bank statements it cannot process.

**How:** - Add a first pass that classifies each document (invoice, bank statement, receipt or payment advice, register, challan, salary sheet, contract note, other) and dispatches it to a per-type schema and extractor under backend/app/extraction/. Reuse document_type plus a cheap call.
- Replace unique=True on vouchers.document_id (models/voucher.py:40-42) with unique(document_id, line_no), with an alembic migration.
- Let services/vouchers.create_for_document (82-88) and queue_missing (108) handle spreadsheet documents, and read every row (parsing/office.py MAX_SHEET_ROWS=500 becomes a preview-only limit).
- Add an engine path that builds PAYMENT, RECEIPT, CONTRA and JOURNAL transactions. The kinds and their Tally names already exist (schemas/canonical.py:33-36, xml_builder.py:21-30).
- Until 4.2 lands, change the upload text at companies/[id]/page.tsx:268 so it no longer invites bank statements.

**Safeguard:** Each line carries an idempotency hash. A batch preview shows counts such as '312 lines: 305 ready, 7 need review'. Unknown document types are parked, not rejected.

**Volume:** Enables everything else in this phase.

### 4.2 Bank statements become Payment, Receipt and Contra entries (XL)

**Removes:** Keying every bank-statement line for every client, and entering bank dates for the bank reconciliation by hand.

**How:** - For CSV/XLSX, detect the header row and map columns once per client and bank layout. For PDFs and scans, use a StatementExtraction schema that returns one row per transaction.
- Choose the bank ledger from the account number, mapped once per client.
- Choose the counter-ledger by matching the narration (name, UPI ID, account number) with match_party, then learned narration rules (a table like LedgerMapping), then rules for bank charges, interest and GST on charges. Transfers between the client's own accounts become Contra.
- Allocate against open bills as Agst Ref (fetch from item 3.7).
- Write the bank date (BANKALLOCATIONS) in xml_builder._ledger_entry.
- Match existing Tally bank vouchers (fetch from item 3.11) and alter their bank dates.

**Safeguard:** Before anything posts:
- Opening balance plus the transactions must equal each printed balance and the closing balance.
- Deduplicate on account, date, amount and UTR across overlapping statements.

When posting:
- Auto-post only lines matched by GSTIN, account number or a learned rule; the rest go to review or Suspense.
- Never invent a party.

**Volume:** Very high: every bank line of every client, probably the largest single block of keying in the office.

**Needs from the owner:** Sample statements (PDF and Excel) from the banks the clients use, and each client's mapping of bank account to Tally ledger, done once.

### 4.3 Import sales and purchase registers and marketplace reports (L)

**Removes:** Keying hundreds of rows from billing-software exports and Amazon or Flipkart reports.

**How:** Add a register importer in services/:
1. Claude proposes a header-to-field mapping, saved per client and header fingerprint, so later files of the same layout need no AI.
2. Group rows by invoice number, build a NormalizedInvoice per invoice, and run the existing build_voucher, validate and post path.
3. Roll B2C rows up to the default walk-in ledger (item 1.15).
4. Read legacy .xls through xlrd.

**Safeguard:** Confirm the mapping once per new layout. Reconcile register totals per tax head against the imported rows. Validate and duplicate-check each row, and preview the batch before posting.

**Volume:** Common, and high per file: hundreds of invoices per register.

**Needs from the owner:** Sample registers from the billing software the clients use.

### 4.4 Receipts, payment advices, and bills already paid in cash or UPI (M)

**Removes:** Typing Receipt and Payment vouchers for customer receipts and payment advices. Also removes entering a Payment voucher to clear every petty-cash or UPI-paid bill that is now booked as a credit purchase, which also creates a new creditor ledger per shop.

**How:** Remove the receipt refusal (accounting/engine.py:282-287).

For receipts and advices: extract the amount, mode, cheque/UTR, bank, invoices settled and TDS deducted, and build RECEIPT or PAYMENT vouchers with Agst Ref (item 3.7).

For bills already paid: add payment_mode and payment_reference to the invoice extraction, and a 'paid from' ledger to LedgerChoices. Such bills become a PAYMENT voucher (expense plus input credit against Cash or Bank). Add a per-client petty-cash rule so small bills from unregistered shops never create a creditor ledger.

**Safeguard:** Require printed evidence of payment. Once bank import exists, link to the bank line instead of booking the payment twice. Deduplicate on party, amount, date window and UTR.

**Volume:** Common.

**Needs from the owner:** Each client's petty-cash ledger and the amount limit for small cash bills.

### 4.5 TDS on expense and service bills (L)

**Removes:** Passing a TDS journal for every 194C/194J/194I/194H/194Q bill and tracking thresholds by hand.

**How:** - Add client TDS settings: deductor or not, rate and threshold per section, and payable ledgers.
- Learn the section per party or expense ledger from the SAC code and the first reviewer pick.
- Take the deductee type from the 4th character of the PAN (the 6th of the GSTIN).
- Keep per-party financial-year totals from posted vouchers.
- The engine adds a credit to 'TDS Payable - 194x' and credits the party the net amount (accounting/engine.py _postings 678-695, party entry 725-733).
- Flag parties without a PAN (higher rate under 206AA), and support lower-deduction certificates.

**Safeguard:** Deduct automatically only when the party's section is set or learned and the threshold is crossed according to posted data; otherwise review. Record the section in the audit trail.

**Volume:** Common: contractor, professional, rent and commission bills for every deductor client.

**Needs from the owner:** Each client's TDS deductor status and TDS ledgers. The CA assigns the section the first time for each party.

### 4.6 GSTR-2B import and purchase reconciliation (L)

**Removes:** The monthly manual comparison of GSTR-2B with the books, and keying purchases that are missing from the books.

**How:** - Accept the 2B JSON or Excel file (ingestion/filetypes.py) and route it by the file's own GSTIN (item 2.2).
- Match on supplier GSTIN, normalised invoice number, date and values against app vouchers and Tally purchases (item 3.11).
- Report three lists: missing in books, missing in 2B, and value mismatches.
- Mark vouchers 'confirmed in 2B'.
- Draft purchase vouchers from 2B rows for parties with a learned item ledger.

**Safeguard:** Drafts for parties without a learned ledger go to review. Respect blocked-credit flags. Deduplicate against uploaded bills with the existing duplicate rules.

**Volume:** Common: monthly for every regular client.

### 4.7 Stock items for trading and manufacturing clients (XL)

**Removes:** Redoing every purchase and sales voucher in Item Invoice mode for clients who keep stock in Tally.

**How:** 1. Add a per-client 'maintains inventory' setting.
2. Sync StockItem, Unit and Godown masters through export_collection_request.
3. Match invoice lines to stock items by alias or part number, by HSN plus fuzzy description, by a learned (party, description)-to-item map, then by an AI pick from the item list. Propose new stock items when nothing matches.
4. For these clients, xml_builder.voucher_request emits ALLINVENTORYENTRIES.LIST with ACCOUNTINGALLOCATIONS, ISINVOICE=Yes and Invoice Voucher View, instead of the accounting view at lines 221 and 239-240.

**Safeguard:** Quantity times rate must equal the line value, and unit conversions must be known. A new item needs an HSN code. Low-confidence lines go to review. The setting is off by default.

**Volume:** Every goods invoice of every client that keeps stock in Tally.

**Needs from the owner:** Which clients keep stock in Tally.

### 4.8 Tax payment challans (M)

**Removes:** Keying GST PMT-06, TDS/TCS (ITNS 281), PF/ESI and advance-tax payments as Payment vouchers every month.

**How:** Add a challan document type and schema: CPIN/CIN, BSR code, period, and tax, interest, fee and penalty per head, plus the bank. Post a PAYMENT voucher debiting the GST cash ledger or payable heads, TDS Payable by section, or PF/ESI Payable, against the bank.

**Safeguard:** Deduplicate on CIN/CPIN and amount. Bank import links to the challan voucher instead of booking the bank line again.

**Volume:** Common: several per client per month.

### 4.9 Salary register (M)

**Removes:** Keying the monthly salary journal and the payment vouchers for clients with staff.

**How:** Import the salary register, mapping its columns once per client. Post one journal: Dr Salary/Wages and employer PF/ESI; Cr Salary Payable and PF/ESI/PT/TDS Payable. Payment vouchers come from bank import.

**Safeguard:** Gross minus deductions must equal net for each row and in total, and net pay must match the bank debit.

**Volume:** Occasional: about one journal per month per client with staff.

### 4.10 Recurring and month-end journals (L)

**Removes:** Typing fixed monthly provisions, rent accruals, loan interest splits, GST set-off and depreciation journals.

**How:** - Recurring-journal templates per client, which the worker drafts on schedule.
- A GST set-off journal computed from Input/Output closing balances fetched from Tally, applying the Section 49 utilisation order.
- Depreciation from a fixed-asset register built from posted Fixed Assets vouchers, at WDV rates.

**Safeguard:** Created as drafts that need one approval. Idempotent with a REMOTEID per client and period, using balances as of the period end.

**Volume:** Occasional: month-end and year-end per client.

**Needs from the owner:** The CA's depreciation policy and recurring provisions per client.

## Phase 5 - Long tail

Close the remaining gaps: less frequent document types, client set-up, and upkeep.

### 5.1 Imports, exports, SEZ and foreign-currency bills (L)

**Removes:** Keying bills of entry, foreign supplier bills and export/SEZ invoices in Tally with their import or export GST details. Also removes fixing overseas party ledgers created with country India.

**How:** - Build on the currency field from item 1.12: convert non-INR bills at a rate printed on the document or the notified rate for the date, and post with Tally's currency and exchange-rate fields.
- Book reverse-charge IGST on imported services.
- Add a bill_of_entry type: BoE number and date, port, assessable value, BCD, SWS and IGST.
- Add Overseas and SEZ registration types and place of supply 96 (normalization/gst.py STATE_CODES); set ProposedLedger.country from the address.
- Derive Composition, SEZ and overseas classifications for parties and vouchers (accounting/engine.py:390, 772).
- Add LUT and shipping-bill fields on export sales.

**Safeguard:** Never post a non-INR bill without a resolved rate, and record the rate's source. Import credit stays in review until reconciled with 2B.

**Volume:** Occasional.

**Needs from the owner:** Which exchange-rate source the office accepts.

### 5.2 Cost centres (M)

**Removes:** Opening each posted voucher in Tally to allocate cost centres for clients that track projects, branches or departments.

**How:** Sync cost categories and cost centres. Allocate by company default, branch GSTIN or address, learned per-party value, or a PO or project reference on the bill. Add allocations to Entry (schemas/canonical.py:79-84) and emit CATEGORYALLOCATIONS.LIST and COSTCENTREALLOCATIONS.LIST in xml_builder._ledger_entry (179-191).

**Safeguard:** Allocate only on ledgers with cost centres enabled. Allocations must sum to the ledger amount. Splits across several centres go to review.

**Volume:** Occasional: only clients that use cost centres.

**Needs from the owner:** Each such client's cost-centre allocation rules.

### 5.3 Use the client's own voucher types and series (S)

**Removes:** Moving entries in Tally to custom voucher types such as 'GST Purchase' or a sales series per branch.

**How:** Fetch VoucherType masters with their parent type during sync. Store a per-company mapping from voucher kind (and optionally sales-number prefix) to type name, and use it in place of the fixed VOUCHER_TYPE_NAMES (xml_builder.py:21-30, 214-225). Pick automatically when exactly one custom type sits under the base type.

**Safeguard:** Fall back to the default type when unsure. Check the mapped type's numbering method before sending VOUCHERNUMBER.

**Volume:** Occasional.

**Needs from the owner:** When a client has several types for the same purpose, which one to use.

### 5.4 Send HSN/SAC, rates, IRN and e-way bill details to Tally (M)

**Removes:** Fixing the HSN summary and 'uncertain transactions' in Tally's GST reports, and typing the IRN, ack number and e-way bill number on e-invoiced sales.

**How:** Send per-entry GST details (HSN/SAC, taxability, rate) on item entries in xml_builder.voucher_request (212-246), or set them on ledgers the app creates. Extract irn, ack_no, ack_date, eway_bill_no and eway_bill_date (from the QR in item 3.2) and send them in Tally's e-invoice and e-way-bill fields. After posting, check Tally's GSTR-1 uncertain-transactions export.

**Safeguard:** Tags differ between Tally releases, so test on a real TallyPrime and fall back to ledger-level details. Validate the 64-character IRN format.

**Volume:** Common for offices that file GSTR-1 from Tally.

### 5.5 Amend or cancel posted entries from the app (M)

**Removes:** Finding a posted voucher in TallyPrime to alter or delete it, after which the app's copy no longer matches Tally.

**How:** Add alter_transaction (re-send with the same REMOTEID and ACTION='Alter') and delete_transaction (ACTION='Delete' or Cancel by REMOTEID) to the connector. Add 'Amend posted entry' and 'Cancel in Tally' actions that set the status from Tally's answer.

**Safeguard:** Read the voucher back first and compare ALTERID and amounts, so changes made in Tally are not overwritten. Reviewers only. Warn or refuse for periods whose returns are filed.

**Volume:** Occasional.

### 5.6 Client set-up in one step: link all Tally companies, opening balances, set-up checks (M)

**Removes:** Adding and syncing each client one at a time, re-linking after a rename in Tally, keying opening balances for clients new to Tally, and finding the Tally address by hand.

**How:** - Add 'Link all open Tally companies', which creates a Company for each list_companies result not yet linked, using its GSTIN, state, the office default rules and an immediate sync.
- Store the Tally company GUID on Company to detect renames, and add screen fields for the Tally name and address.
- Import the previous trial balance and debtor/creditor ageing to set opening balances and opening bills.
- Have start.ps1 probe 127.0.0.1:9000 for Tally.
- Show a header warning when no AI key is set.

**Safeguard:** The admin confirms the list and unticks test copies. Match by GUID so nothing is linked twice. The trial balance must balance. Opening entries need one approval.

**Volume:** One-time per client.

**Needs from the owner:** Confirm which Tally companies are real clients.

### 5.7 Several GST registrations, and a new Tally company each year (M)

**Removes:** Changing the GST registration on vouchers for multi-state clients, re-linking clients to a new year's Tally company, and keying previous-year bills by hand after the switch.

**How:** - Sync every registration into a company_registrations table (xml_parser.py:122 reads only the first today). Recognise the company by any of its GSTINs, and post under the matching registration (CMPGSTIN at xml_builder.py:198) with its own voucher type and tax ledgers.
- Allow several Tally companies per client, each with its books period (BOOKSFROM is already fetched), and pick the target by voucher date.
- Keep learned mappings and duplicate history at client level.

**Safeguard:** A bill matching no registration, or several, goes to review, and the primary GSTIN is never used silently. Never post to a company whose books period excludes the date.

**Volume:** Occasional: multi-state clients and the yearly company switch.

### 5.8 Clients whose Tally can't be reached, or who use other software (L)

**Removes:** Keying bills by hand for clients on a remote or offline Tally, or on Busy, Marg, Zoho Books or QuickBooks.

**How:** Add an 'Export for Tally' action that writes the existing create_ledgers_request and voucher_request XML into one import file and tracks the export state. Optionally, a small agent at the client site fetches queued vouchers over HTTPS. Select the connector from company.connector_type (stored but unused; services/connectors.py:12-13 always returns TallyConnector) and add Busy, Zoho Books and QuickBooks connectors plus a generic CSV export.

**Safeguard:** Keep REMOTEID so a re-imported file alters rather than duplicates. Check ledger names against the client's latest master export. Each new connector starts with review on and has its own tests.

**Volume:** Occasional: clients with remote or non-Tally books.

**Needs from the owner:** Which other accounting software the clients use.

### 5.9 Complete party masters: PAN, MSME, contact details and credit period (S)

**Removes:** Opening each app-created ledger in Tally to type the PAN, MSME/Udyam number, TDS details, email and phone, and typing credit days into each bill allocation.

**How:** - Derive the PAN from a valid regular GSTIN and send it as INCOMETAXNUMBER in create_ledgers_request.
- Extract the Udyam number, email, phone, due date and payment terms.
- Send BILLCREDITPERIOD in the bill allocation (xml_builder.py:186-191), falling back to a credit period learned per party.
- Fetch these fields at sync so the MSME and TDS rules can use them.

**Safeguard:** Never derive a PAN from a TDS-deductor GSTIN. Set the Udyam number only on the exact format with high confidence. Leave out low-confidence due dates.

**Volume:** Common: every new ledger.

### 5.10 Safer upkeep: off-site backups and self-updating (S)

**Removes:** Downloading backup copies by hand, copying the stored invoice files separately, the 4-step manual restore, and manual file-replacement updates with 'start.bat -Rebuild'.

**How:** - Add an off-site target setting (second drive, NAS or synced cloud folder). After each scheduled backup (services/backups.py), copy the .db there and mirror backend/data/storage; it is content-addressed, so this is incremental. Run an integrity check on the copy and alert when the newest good backup is more than 2 days old.
- Add 'start.ps1 -Restore <name>'.
- Make devtools/upgrade.py return non-zero when it stops.
- In start.ps1, run npm ci when package-lock.json changes, and rebuild when web sources are newer than BUILD_ID.
- Optionally add a signed-release updater scheduled outside office hours.

**Safeguard:** Encrypt off-site copies, never prune the only verified copy, and test-restore to a scratch path first. Keep the previous version for rollback, and roll back automatically if /api/health fails after an update.

**Volume:** Daily backups; rare updates.

**Needs from the owner:** Where off-site copies should go.

### 5.11 Investment and income-tax documents (L)

**Removes:** Keying broker contract notes and mutual-fund capital-gains statements into Tally, and Form 16, AIS/TIS and 26AS into return software.

**How:** Add document types with their own schemas (through the classifier in item 4.1). A contract note becomes trade lines plus charges, posted as investment purchase/sale journals according to a per-client choice of investment or stock-in-trade. Form 16, AIS and 26AS go into a structured income-tax data sheet exported for the office's return software. Accept AIS JSON at intake.

**Safeguard:** The accounting treatment of investments is a per-client setting, and the first entries are reviewed. Contract-note totals must match the net amount payable or receivable.

**Volume:** Common in ITR season.

**Needs from the owner:** Which return software the office uses, and its import format.

## What stays with a person

- **Paper bills must be scanned or photographed, and clients must send their documents**
  - Why: No software can read paper it never receives.
  - Keep it rare: Give clients phone, WhatsApp and email channels that flow straight in (2.1), and a scanner that saves to a watched folder. The app chases missing bills automatically, using 2B mismatches (4.6) and 'clearer copy please' requests (3.3).
- **TallyPrime must be running, licensed (not in Educational mode), with its XML server on and the client's company loaded**
  - Why: The app's connector can only list companies that are already loaded; it has no request that loads a company. The licence and Educational mode live in Tally's own interface.
  - Keep it rare: Use a Tally startup configuration that loads all active clients at once. Entries wait instead of failing (1.8), and the screen and one alert say exactly which company to open (1.10).
- **The CA's decision to trust automatic posting for each client, and review of entries above an amount limit**
  - Why: Posting is professional responsibility, and a wrong entry in Tally is hard to undo.
  - Keep it rare: The trust ramp proposes graduation once a client has a clean history (1.2). Use a daily digest and random sample checks instead of reviewing everything, and switch back to review automatically if corrections rise.
- **Genuinely unreadable documents (torn, blurred, faded thermal paper, handwritten over)**
  - Why: The figures are not recoverable from the image.
  - Keep it rare: Preprocess the image, run a targeted second read and crop re-reads (3.3), and send an automatic request to the client for a clearer copy before anyone types.
- **First-time judgement calls: a new party without a GSTIN, a new expense head, capital versus revenue on a large item, a party's TDS section, a new stock item, a new register or bank layout, a client's bank account mapping**
  - Why: The information is not on the document, or is a professional choice.
  - Keep it rare: Learn each decision once per party or layout, then apply it automatically. Group similar pending items so one decision clears all of them. Ask for capital versus revenue only above a value limit.
- **Ambiguous duplicates and routing: same invoice number with a different amount or date, revised invoices, documents that match no client or several clients**
  - Why: Either document could be the real one, or the owner cannot be identified.
  - Keep it rare: Close exact duplicates automatically (1.13), route by exact GSTIN only (2.2), and show the candidates side by side so the decision takes one click.
- **Bank lines with no identifiable payee: cash deposits, bare 'TRF' entries, personal versus business spending**
  - Why: The nature of the transaction is not in the data.
  - Keep it rare: Use learned narration rules (4.2). Park unknown lines in Suspense with a month-end list for the client, so each pattern is answered once.
- **Tax and legal judgements: GTA forward or reverse charge option, Section 17(5) personal use, year-end cut-off, provisions and depreciation policy, changes to periods already filed**
  - Why: These are CA judgements with interest and disallowance consequences.
  - Keep it rare: Encode them as rules the CA confirms once (3.9, 3.10). Draft the journals for one approval (4.10). Refuse automatic changes in filed periods.
- **One-time set-up: confirming which Tally companies are clients, each client's GST status and registrations, an Anthropic key with billing, alert recipients**
  - Why: These facts and accounts sit outside the documents.
  - Keep it rare: Link all companies in bulk with values pre-filled from Tally (5.6), and read GST status from Tally where possible (1.16).
- **Following up with suppliers about invoices missing from 2B, or wrong GSTINs on their bills**
  - Why: Needs a conversation with a third party.
  - Keep it rare: Generate the mismatch report and a draft message per supplier automatically (4.6).
