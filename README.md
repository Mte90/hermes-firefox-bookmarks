# hermes-plugin-firefox-bookmarks

Plugin per [Hermes Agent](https://hermes-agent.nousresearch.com) che legge, cerca e analizza i **preferiti di Firefox** — dal cloud di **Firefox Sync** (Sync 1.5, end-to-end encrypted) o da un profilo locale (`places.sqlite`), senza Firefox installato sulla macchina. **v1 read-only**: non modifica i preferiti sul server.

```
Hermes Agent ──(tool / skill / comandi)── firefox-bookmarks
                                            │
                        ┌───────────────────┴──────────────┐
                        │ cache SQLite + FTS5 (locale)     │  ← cache-first:
                        └───────────────────┬──────────────┘    ogni query è offline
                            sync on-demand  │
                   ┌────────────────────────┴─────────────────┐
                   │ Firefox Sync 1.5 (FxA + HAWK)            │
                   │ o places.sqlite (modalità local)         │
                   └──────────────────────────────────────────┘
```

## Caratteristiche

- **Sync**: login Firefox Account (con 2FA/TOTP), HAWK, HKDF-SHA256, AES-256-CBC+HMAC sui BSO — implementato in Python puro (`ffsync/`, zero dipendenze da Hermes)
- **Search**: FTS5 su titoli/URL/tag/struttura cartelle + filtri dominio, cartella, tag, periodo; query naturali ("mi avevo salvato qualcosa su systemd?")
- **Analyse deterministiche** (no LLM): conteggi, distribuzione per dominio, duplicati, link orfani (URL senza title)
- **Link check** opt-in: HEAD concorrente (4 thread), timeout, politeness; mai automatico
- **Modalità local**: legge `places.sqlite` in sola copia (zero rischio sul profilo)
- **Skill** `firefox-bookmarks` con regole operative (query corte, consenso per link-check)

## Requisiti

- Hermes Agent recente con sistema plugin (user dir: `~/.hermes/plugins/` o `$HERMES_HOME/plugins/`)
- Python 3.10+ nel venv Hermes (già incluso in installazioni standard)
- Runtime: `httpx` + `cryptography` (già nel venv Hermes); dev: `pytest`

## Installazione

```bash
git clone <URL-repo> ~/.hermes/plugins/firefox-bookmarks
# oppure: unzip in ~/.hermes/plugins/
```

Attiva il plugin (il gateway rileva i user plugin in `plugins/`):

```bash
hermes plugins list          # verifica che firefox-bookmarks compaia
hermes plugins doctor firefox-bookmarks
```

Il plugin richiede **0 capability privilegiate** e **non sovrascrive tool built-in**.

## Prima configurazione (una tantum)

Le credenziali **non passano mai da Telegram/LLM** (la password va solo nel login FxA; il refresh token resta su disco con permessi `600`).

**Piano A — Firefox Sync (richiede un Firefox Account con Sync attivo):**

```bash
cd ~/.hermes/plugins/firefox-bookmarks
python -m ffsync.probe setup          # password da terminale (getpass, non visibile)
python -m ffsync.probe sync           # primo sync + verifica
python -m ffsync.probe status         # stato credenziali + cache
```

**Piano B — profilo locale (senza account):**

```bash
python -m ffsync.probe local ~/.mozilla/firefox/<profile>/  # o path a places.sqlite
```

## Uso

Dopo il primo sync (o con il piano B), tutto funziona in chat:

- «Cosa ho salvato su systemd?» → `firefox_bookmarks_search`
- «Quanti bookmark ho e dove sono di più?» → `firefox_bookmarks_analyze`
- «Mostrami la struttura delle cartelle» → `firefox_bookmarks_tree`
- «Controlla se i link di una cartella sono morti» → `firefox_bookmarks_recheck` (richiede consenso)
- Comandi: `/bookmarks-sync` (refresh immediato), `/bookmarks-report` (overview + domini + duplicati)

## Sincronizzazione: ogni quanto?

**Non esiste una cadenza fissa nel plugin: il sync è on-demand** (tool `firefox_bookmarks_sync`) o **schedulabile** con un job Hermes.

Comportamento concreto v1:

- `firefox_bookmarks_search`/`analyze`/`tree` **non** innescano mai il sync: leggono la cache locale. Se i dati hanno >7 giorni la risposta include `hint: consider sync`.
- Il sync non è incrementale in v1 (full-list di `bookmarks`): è leggerissimo (<1000 record) ma resta meglio non eseguirlo a ogni query.

Cadence consigliate:

| Esigenza | Setup |
|---|---|
| «sempre aggiornati» | job ogni **30 minuti** (vedi sotto) |
| uso quotidiano | job **ogni notte** (es. 03:00) — è l'esempio del piano di sviluppo |
| occasionale | solo on-demand (`/bookmarks-sync`) |

Esempio di job (linguaggio naturale, Hermes crea il cron job):

```
ogni 30 minuti, se la cache dei bookmarks è vuota o ha più di 30 minuti,
esegui firefox_bookmarks_sync e non rispondere se tutto è ok
```

Nota: ogni sync consuma quota FxA (i rate limit di Sync sono per-collection; la
paginazione gestisce i 429 con backoff). Un job ogni 30 minuti è ragionevole;
ogni 5 minuti no.

## Struttura

```
firefox-bookmarks/
├── plugin.yaml                  # manifest Hermes
├── __init__.py                  # register(ctx) — tool/skill/comandi
├── tools.py                     # JSON Schema + handler dei 5 tool
├── cache.py                     # cache SQLite + FTS5 (schema §8 del piano)
├── analysis.py                  # stats deterministiche
├── linkcheck.py                 # verifica link morti (opt-in)
├── ffsync/                      # libreria Firefox Sync, zero dipendenze Hermes
│   ├── crypto.py                # PBKDF2/HKDF/AES-CBC/HMAC
│   ├── auth.py                  # login FxA + 2FA + credenziali su disco
│   ├── storage.py               # REST Sync 1.5 (HAWK, paging)
│   ├── bookmarks.py             # parser record → dataclass + albero cartelle
│   ├── local.py                 # modalità B: places.sqlite
│   └── probe.py                 # CLI setup/status/sync/local
├── skill/firefox-bookmarks/SKILL.md
├── tests/test_plugin.py         # 13 test offline
├── README.md · CHANGELOG.md · LICENSE (MIT)
```

## Test

```bash
cd firefox-bookmarks
python -m pytest tests -q      # 13 test offline (crypto, HAWK, parser, cache, handler)
```

## Troubleshooting

| Sintomo | Causa / rimedio |
|---|---|
| `NOT_CONFIGURED` | Esegui `python -m ffsync.probe setup` |
| `SESSION_EXPIRED` | Il refresh token è scaduto: `probe setup` di nuovo |
| `ACCOUNT_LOCKED` | Mozilla ha bloccato dopo troppi tentativi: attendi e riprova |
| `NEEDS_VERIFICATION` | L'account FxA non ha completato la verifica email: completa su accounts.firefox.com |
| `STORAGE_ERROR 429` | Rate limit Sync: già gestito con backoff; evita sync ravvicinati |
| La cache sembra ferma | Controlla `stats.last_sync` (epoch) e lancia `/bookmarks-sync` |

## Sicurezza

- **Read-only v1**: nessuna scrittura verso il server Sync.
- La password non viene salvata: solo `refreshToken` + `keyFetchToken` (su disco, `600`).
- Le credenziali non transitano mai nei log, nelle risposte dei tool o nei job schedulati.
- Link-check: mai automatico; richiede richiesta esplicita (vedi skill).
- Dati: i preferiti restano end-to-end encrypted su Firefox Sync; la cache locale è in chiaro su disco — se condividi la macchina, proteggi la cartella `plugin-data/`.

## Licenza

MIT — vedi [LICENSE](LICENSE).

## Stato e next steps (dopo v1)

- [ ] Sync incrementale (solo BSO `modifiedAfter` recenti)
- [ ] Fix di chiavi vecchie (chiavi "oldsync" 2012–2017)
- [ ] Categorizzazione tematica con LLM (batches, risultati in cache)
- [ ] Link-check schedulato + report settimanale
- [ ] Porting a MCP server per altri agenti