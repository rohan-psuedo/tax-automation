# Installing Tax Automaton in your office

Tax Automaton runs on one Windows computer in your office, usually the one where TallyPrime
runs. Everyone else opens it in their web browser. Your invoices and books stay on that
computer; only the invoice files are sent to the AI service you choose (such as Claude,
Gemini or OpenAI) so that it can read them.

## What you need

- A Windows 10 or 11 computer that stays on during office hours.
- **uv**: install it from https://docs.astral.sh/uv/ (one command, shown on that page).
- **Node.js**, the LTS version: install it from https://nodejs.org.
- **TallyPrime**, with the companies whose books you keep.
- An **API key from an AI service** for reading invoices: Claude, Gemini, OpenAI,
  OpenRouter or one of the others listed under *Set up invoice reading* below. Without a key
  the app still works, but every invoice has to be typed in by hand on the review screen.

## Start the app

Double-click **start.bat** in the Tax Automaton folder. The first start takes a few minutes
while it installs and builds everything; later starts take a few seconds.

When the window says *Tax Automaton is running*, your browser opens at
http://localhost:3000. **Keep that window open** while the app is in use. To stop the app,
click the window and press Ctrl+C.

To let other computers in the office use it, start it with:

```
start.bat -Lan
```

The window then shows the address to open on the other computers, such as
`http://192.168.1.20:3000`. Windows may ask whether to allow Node.js through the firewall
on private networks: allow it.

## First-time setup in the app

1. **Create the administrator account.** The first visit asks for your name, email and a
   password.
2. **Connect Tally.** In TallyPrime, press Alt+Z (Exchange), then *Configure* ›
   *Client/Server Configuration* (older TallyPrime releases: F1 › *Settings* ›
   *Connectivity*). Set *TallyPrime acts as* to **Both** and the port to **9000**, and keep
   a company open. In the app, open **Settings** › *Tally and AI*, check the Tally address
   (use `127.0.0.1:9000` when Tally is on the same computer) and click **Test connection**.
3. **Set up invoice reading.** On the same screen, under *Invoice reading (AI)*, paste the
   API key of your AI service and click **Save key**, then **Test AI connection**. The app
   recognises which service a key belongs to. See *Set up invoice reading* below for where
   to get a key.
4. **Add your team** under **Settings** › *Team*. Administrators manage settings, the team
   and companies; Reviewers check entries and post them to Tally; Preparers upload and
   correct documents.
5. **Add the companies** you keep books for (Companies › *Add company*, then pick the
   company from the list Tally shows), and click **Sync ledgers from Tally** on each.

## Set up invoice reading

The app reads invoices with an AI service of your choice. You need an **API key** from that
service: a ChatGPT, Gemini or Claude app subscription is not enough, because API use is
billed separately, by the service, for each document read. Create the key in the service's
own account pages:

| Service | Where to create the key | The key starts with |
|---|---|---|
| **Claude** (Anthropic) | console.anthropic.com › *API keys* | `sk-ant-` |
| **Gemini** (Google) | aistudio.google.com › *Get API key* | `AIza` or `AQ.` |
| **OpenAI** (GPT) | platform.openai.com › *API keys* | `sk-` (often `sk-proj-`) |
| **OpenRouter** (many models with one key) | openrouter.ai › *Keys* | `sk-or-` |
| Groq | console.groq.com › *API Keys* | `gsk_` |
| Grok (xAI) | console.x.ai › *API Keys* | `xai-` |
| DeepSeek | platform.deepseek.com › *API keys* | `sk-` |
| Mistral | console.mistral.ai › *API Keys* | (no fixed start) |
| Other (OpenAI-compatible) | Together, Fireworks, or Ollama / LM Studio on your network | (any) |

Then, in the app:

1. Open **Settings** › *Tally and AI*. Under *Invoice reading (AI)*, paste the key in the key
   box and click **Save key**. The app recognises the service from the key (the service list
   switches to it) and from then on reads invoices with that service. For a Mistral key, or
   one for the *Other* service, choose the service in the list first, then paste the key.
2. **Model.** Each service starts with a suggested model, which suits invoices. To use
   another, pick it from the list (once the key is saved, the list includes the models your
   key can use) or type its name exactly as the service writes it, then click **Save
   changes**. For Claude you can also set the *Effort*.
3. Click **Test AI connection**. It checks the key and the model without reading a document.

For the *Other (OpenAI-compatible)* service, also enter the service's address (it usually
ends in `/v1`, for example `http://192.168.1.30:11434/v1` for Ollama on another computer) and
the model name. A server on your own network may not need a key.

You can save keys for several services and switch between them with **Use … for reading**;
each keeps its own key and model. Keys are stored encrypted, and the screen only ever shows
their first and last few characters.

**Documents uploaded before the key was added are read automatically** once it is saved, as
long as nobody has started typing them in. The same happens after a key was rejected and
you save a new one, and for scans that a text-only service (DeepSeek) could not read once
you switch to a service that reads scans.

**Privacy.** Each invoice file is sent to the service in use, which processes it under its
own terms. Check the service's data policy before sending client documents: some free plans
let the service use what you send to improve its models, so use a paid key for client work.
With a model running on your own network (Ollama or LM Studio, through *Other*), documents
never leave the office. How well invoices are read, and what it costs, depends on the
service and model, so check the first entries closely after you change either.

## Everyday use

Upload invoices on a company's **Documents** tab. Each one is read, turned into an entry,
and checked. Open an entry to review it, correct anything marked, and click **Post to
Tally**, or use **Post ready entries to Tally** for all entries with no issues.

Each company's posting rules are on its **Ledgers & rules** tab. *Review every entry before
posting* is on for new companies; once you trust the results, you can turn it off and turn
on *Post clean entries to Tally automatically*.

## Backups

The app backs up its database about once a day, and before every update, into
`backend\data\backups`. You can also make one under **Settings** › *Backups*, and download
it to keep a copy elsewhere. Uploaded files are kept in `backend\data\storage`: include that
folder in your usual file backup. How to restore a backup is explained on the Backups
screen.

## Updating

Replace the program files with the new version (never delete the `backend\data` folder:
it holds your data), then start the app with:

```
start.bat -Rebuild
```

The database is backed up automatically before it is updated.

## If something goes wrong

- *Tally not reachable* at the bottom of the screen: check that TallyPrime is open, with a
  company loaded and the connectivity settings above, then click **Test connection** in
  Settings.
- *… is not open in Tally*: open that company in TallyPrime and post again. The app only
  posts to a company that is open, because Tally can otherwise put the entry into whichever
  company is active.
- *Tally already has a ledger named …*: someone created that ledger in Tally since the last
  sync. Click **Sync ledgers from Tally**, then choose the ledger on the entry. (Creating it
  again would overwrite the one in Tally.)
- *Voucher date is missing* from Tally, for an entry that has a date: TallyPrime is in
  Educational mode, which only accepts vouchers dated the 1st, 2nd or 31st of a month. A
  licensed TallyPrime switches to Educational mode when it can't reach its licence.
- *Invoice reading is not set up*: no key (or, for the *Other* service, no address or model)
  is saved for the AI service in use. Add it in Settings › *Tally and AI*; the waiting
  documents are then read automatically.
- *The … API key was rejected*: the key was copied incompletely or has been revoked. Create
  a new key and save it (the documents that failed are then read again), and click **Test
  AI connection**.
- *… can only read text*: DeepSeek can't read scans or photos. Choose another service, or
  type the document in.
- *Too many failed sign-ins*: wait 15 minutes, or ask an administrator to reset the password
  under Settings › Team.
- The app keeps a log in `backend\data\logs\app.log`. Send that file along when you ask for
  help.
