# Tesla

Lokalni glasovni asistent za GNOME (Wayland). Pritisneš prečicu, kažeš šta ti treba, a Tesla odgovori glasom. Usput može da otvori aplikaciju, sajt, terminal ili VS Code, ili da pronađe nešto u tvojim projektima. Mozak je Claude Code preko tvoje Claude subskripcije. Govor se prepoznaje preko Groq-ovog Whisper-a (`whisper-large-v3`, besplatno uz `GROQ_API_KEY`). Kad Groq nije dostupan, preuzima lokalni model (whisper.cpp na iGPU, ili faster-whisper na CPU-u). Glas su Microsoftovi srpski neural glasovi preko interneta (edge-tts).

## Instalacija

```bash
scripts/install.sh
```

Skripta instalira Teslu kao zaseban program (`uv tool`), uz notch ekstenziju, prečicu `Super+Alt+Space` i systemd korisnički servis. Tesla se tako pokreće pri svakoj prijavi, a ako padne, servis je sam podigne. Prvi put traje nekoliko minuta, jer pravi i whisper.cpp sa Vulkanom za iGPU, bez `sudo`.

- **Nova verzija:** posle izmena u repou pokreni `scripts/install.sh` ponovo. Zameniće verziju koja radi i restartovati servis. Sve dok to ne uradiš, rad u repou ne dira Teslu koja radi. Ako se promenila i ekstenzija, skripta će ti reći da se odjaviš i ponovo prijaviš.
- **Komande:** `tesla version`, `journalctl --user -u tesla -f` za logove, `systemctl --user restart tesla`.
- **Uklanjanje:** `scripts/uninstall.sh`. Konfiguracija i modeli ostaju.

**Za razvoj** pokreni `uv run tesla` iz repoa. Prvo zaustavi servis (`systemctl --user stop tesla`), jer dve Tesle ne mogu da rade istovremeno. Druga se odmah ugasi sa porukom da Tesla već radi.

**Kratak pritisak na `Super+Alt+Space`:** Tesla sluša dok ne ućutiš.

**Držanje tastera:** ako posle pritiska nastaviš da držiš `Super+Alt`, sluša sve dok ne pustiš, i pauze joj tada ne smetaju.

Može i bez prečice. Kaži "Hej Tesla" i odmah nastavi ("Hej Tesla, otvori terminal"), ili napravi pauzu pa kaži zahtev.

- Buđenje glasom isključuješ sa `tesla wake off` ili glasom "isključi buđenje".
- Ponovo ga uključuješ sa `tesla wake on`.
- Trajno: `wake.enabled = false`.
- Dok je uključeno, mikrofon je stalno otvoren, pa GNOME stalno prikazuje indikator mikrofona. Ništa se ne snima niti šalje dok ne čuje ime.

- **Ponovni pritisak dok slušaš:** odmah završava snimanje.
- **Ponovni pritisak dok priča ili razmišlja:** prekida je i ponovo sluša.
- **Posle odgovora** još nekoliko sekundi čeka nastavak bez prečice (`listen.followup_s`).

Notch iskače ispod sata:

- **Slušanje:** pokazuje nivo mikrofona.
- **Razmišljanje i rad alata:** pokazuje šta Tesla trenutno radi ("Čitam fajl…").
- **Odgovor:** pitanje i odgovor se vide dok stižu.
- **Kod, putanje i vrednosti:** dobijaju poseban blok sa dugmetom za kopiranje.
- **Rizične radnje:** pojavljuje se kartica sa dugmićima "Dozvoli" i "Odbij".

Bez ekstenzije sve radi i dalje, samo preko notifikacija.

**Kontekst ekrana.** Za svako obraćanje ekstenzija beleži:

- aktivni prozor (naslov i aplikaciju);
- selektovan tekst, ako je selektovan u poslednjih 90 s;
- snimak ekrana: podrazumevano monitora na kom je kursor miša (`[screen] capture`: `pointer`, `focused` za monitor aktivnog prozora, `primary` za glavni monitor, `all` za sve monitore u jednoj slici).

**Kada nastaje:**

- **prečica:** u trenutku pritiska, pre nego što notch iskoči;
- **"Hej Tesla":** odmah posle buđenja;
- **nastavak razgovora:** čim završiš rečenicu, paralelno sa prepoznavanjem govora, pa ne dodaje kašnjenje;
- **kucani zahtev:** pre slanja modelu.

Kad je notch vidljiv, ne sakriva se (nema treptanja), nego se njegovo mesto na snimku prekrije tamnim pravougaonikom.

Snimak ostaje lokalno u `$XDG_RUNTIME_DIR/tesla/` (tmpfs, svaki novi prepisuje stari). Modelu ide samo kad u rečenici pomeneš ekran ("šta je ovo", "ova greška", "na ekranu") ili kad model sam pozove `look_at_screen`.

Glasovne komande koje ne idu do modela:

| Kažeš | Šta se desi |
|---|---|
| "nova tema" / "počnimo ispočetka" | nova konverzacija |
| "pređi na sonet" / "pređi na haiku" / "pređi na opus" | promena modela, kontekst ostaje |
| "stani" / "nema veze" | prekid |
| "hvala, ćao" / "to je sve" / "laku noć" | kratak pozdrav, pa mirovanje i skrivanje notch-a |
| "otvori drugi link" / "otvori sve izvore" | otvara izvore iz poslednjeg odgovora |
| "isključi buđenje" / "uključi buđenje" | "Hej Tesla" isključeno ili uključeno |

**Glas:** `[tts] voice = "sophie"` za ženski glas, `"nicholas"` je podrazumevani. Brzina se podešava preko `rate = "+10%"`. Preslušavanje: `uv run tesla say --voice sophie "Zdravo"`.

Testiranje pojedinačnih delova:

```bash
uv run tesla say "Zdravo, ja sam Tesla."     # samo glas
uv run tesla hear                             # mikrofon + VAD + Whisper
uv run tesla ask "Koji port koristi DJS_Backend?" [--speak]   # samo model
uv run tesla send "Otvori Firefox"            # kucani zahtev pokrenutoj Tesli (sa notch-om)
scripts/nested-shell.sh                       # razvoj ekstenzije u GNOME-u u prozoru
uv run scripts/bench_voice.py --record        # Whisper modeli na tvom glasu
uv run scripts/bench_llm.py                   # brzina modela kroz claude CLI
```


## Bezbednost

Svaki poziv alata prolazi kroz `src/tesla/policy.py`:

- **Slobodno:** čitanje i pretraga fajlova, web, Bash komande koje samo čitaju (`ls`, `cat`, `git status`, `git log`…) i Teslini alati za otvaranje aplikacija, sajtova, foldera i terminala.
- **Uz odobrenje** (notifikacija sa dugmićima "Dozvoli" i "Odbij"): bilo kakav upis ili izmena fajla, svaka druga Bash komanda, čitanje `.env` fajlova.
- **Nikad:** `sudo`, brisanje `/` ili `~`, `mkfs`, `dd` na disk, kao i bilo kakav pristup `~/.ssh`, `~/.gnupg`, keyring-u, cloud kredencijalima i kolačićima browsera.

Tesla pokreće Claude Code izolovano: bez tvojih korisničkih podešavanja, MCP servera i CLAUDE.md fajlova, sa sopstvenim workspace-om u `~/.local/share/tesla/workspace`. `ANTHROPIC_API_KEY` se namerno uklanja iz okruženja, da bi se uvek koristila subskripcija.

Claude Code čuva transkript svake sesije, uključujući priložene snimke ekrana, u `~/.claude/projects/-home-vuk--local-share-tesla-workspace/`. Tesla ga briše čim se konverzacija završi ("nova tema", 15 minuta neaktivnosti, gašenje daemona). Pri pokretanju daemona briše i sve zaostale transkripte.

## Konfiguracija

Pogledaj `config.example.toml`. Konfiguracija se čita iz `~/.config/tesla/config.toml`, a svaki ključ je opcion.

## Arhitektura

```
Super+Alt+Space ─► ekstenzija (dev.vuk.TeslaShell): notch, kontekst, screenshot, hold-to-talk
                     │  Activate(kontekst) / StopListening / Approve      ▲ Event(vrsta, vrednost)
                     ▼                                                    │
                   daemon (dev.vuk.Tesla) ────────────────────────────────┘
                           │
  mikrofon ─► Silero VAD ─► faster-whisper (sr) ─► Claude Code (Agent SDK, stream-json)
                                                      │  alati: Read/Glob/Grep/Bash/Web + mcp__tesla__*
  zvučnik ◄── Edge TTS (sr-RS) ◄── rečenica po rečenicu ◄┘  dozvole: policy.py
```

| Modul | Uloga |
|---|---|
| `daemon.py` | mašina stanja, D-Bus servis, tok jednog obraćanja |
| `brain.py` | jedan dugoživeći `claude` proces, prekid, promena modela, reset posle neaktivnosti |
| `policy.py` | dozvole za alate |
| `desktop.py` | MCP alati: `open_app`, `open_url`, `open_path`, `open_terminal`, `open_in_vscode`, `notify` |
| `text.py` | ćirilica u latinicu, deljenje na rečenice za TTS, ``` blokovi se prikazuju umesto da se izgovaraju |
| `wake.py` | "Hej Tesla": Vosk detektor; skidanje imena sa početka Whisper transkripcije |
| `screen.py` | kontekst ekrana, odluka kada se šalje screenshot, alat `look_at_screen` |
| `gnome-extension/tesla@vuk.dev` | GNOME Shell 46 ekstenzija (notch, prečica, hvatanje konteksta) |

## Merenja (Ryzen 7 5700U, CPU)

**Modeli kroz `claude` CLI, sa toplim procesom (medijana):**

| Model | Prvi token | Prva rečenica |
|---|---|---|
| **Haiku 5.5, effort low (podrazumevano od 8.10.)** | **0,57 s** | **0,9 s** |
| Haiku 4.5, razmišljanje isključeno | 0,55 s | 1,0 s |
| Sonnet 5.5, effort low | 1,1 s | 2,1 s |
| Opus 5.5, effort low | 1,6 s | 2,1 s |
| Haiku 4.5, podrazumevano (razmišlja) | 1,8 s | 2,2 s |

**STT na sintetičkom srpskom (stari Piper glas, CPU):**

| Whisper model | Vreme po rečenici | WER |
|---|---|---|
| `tiny` | 0,4 s | ~80% |
| `base` | 0,7 s | ~60% |
| `small` | 1,6 s (8 niti) | 52–59% |

Na Vukovom glasu `small` je upotrebljiv: LLM razume i transkripcije sa greškama. `tiny` i `base` su neupotrebljivi. Parakeet v3 ne podržava srpski.

Whisper uvek obrađuje prozor od 30 s, pa na CPU-u svako obraćanje traje oko 1,4–1,8 s bez obzira na dužinu.

**Groq naspram lokalnog STT-a, Vukov glas, 6 rečenica:**

| | Vreme po rečenici | WER |
|---|---|---|
| **Groq `whisper-large-v3` (podrazumevano)** | **0,32 s** | **16%** |
| Groq `whisper-large-v3-turbo` | 0,24 s | 19% |
| lokalni `large-v3-turbo` na iGPU (rezerva) | 1,54 s | 26% |

**Groq** (`[stt] service = "groq"`):

- **Ključ:** traži `GROQ_API_KEY` u `.env` u repou. `install.sh` ga kopira u `~/.config/tesla/env`.
- **Rezerva:** lokalni model je uvek učitan i preuzima kad nema ključa, kad Groq vrati grešku, kad ne odgovori za `groq_timeout_s` ili kad se potroši ograničenje (429). Posle ograničenja Tesla do isteka zadatog vremena (`retry-after`) ide pravo na lokalni model.
- **Privatnost:** tvoj glas tada ide Groq-u. Sa `service = "local"` sve ostaje na računaru.

**STT na Vukovom glasu, whisper.cpp na iGPU (Radeon Vega, Vulkan), 6 rečenica:**

| Model | Prozor | Vreme po rečenici | WER |
|---|---|---|---|
| faster-whisper `small`, CPU (rezerva) | 30 s | 1,9 s | 37% |
| `small` q8 | 30 s | 0,8 s | 43% |
| `medium` q5 | 30 s | 2,3 s | 24% |
| **`large-v3-turbo` q8 (podrazumevano sa GPU-om)** | **≥15 s** | **1,6 s** | **20%** |
| `large-v3-turbo` q8 | 30 s | 3,4 s | 9% |

- **Uži prozor ruši veće modele:** `turbo` sa prozorom ispod 15 s izbacuje besmislice ili engleski, a `medium` upada u petlje ponavljanja.
- **Bez GPU-a je `turbo` neupotrebljiv:** na CPU-u traje oko 11 s.
- **`small` modeli trenirani na srpskom** (Sagicc, istomin) su na CPU-u dali 30–34% grešaka, uz isto vreme kao `small`.
- **flash attention ne pomaže** na Vulkanu.

**TTS:** Microsoftovi neural glasovi `sr-RS-NicholasNeural` (podrazumevani, muški) i `sr-RS-SophieNeural` (ženski). Prvi zvuk stiže za oko 0,5 s po rečenici.

- **Ćirilica:** ovi glasovi latinicu čitaju kao besmislicu, pa se sve izgovara u ćirilici. Engleske reči prvo dobiju srpski izgovor ("Firefox" postaje "fajerfoks", "VS Code" "vi es kod").
- **Bez interneta:** odgovori se samo prikazuju u notch-u, bez glasa.
- **Piper (`sr_RS-serbski_institut`, lokalno) je izbačen:** razumljivost kroz STT bila mu je 47–61%, prema 26% kod Edge glasova, a 26% je praktično donja granica tog merenja.

## Plan

1. ~~Faza 0/1: merenja i glasovna petlja bez UI-a~~
2. ~~GNOME Shell ekstenzija: notch, hold-to-talk, odobravanje, kontekst ekrana~~
3. ~~Brži i tačniji STT: whisper.cpp + large-v3-turbo na iGPU (Vulkan)~~
4. Memorija (profil + istorija + dnevna refleksija)
5. ~~Wake word "Hej Tesla"~~ (Vosk + Whisper; kasnije eventualno istreniran openWakeWord model), diktiranje, delegiranje poslova Claude Code-u, tajmeri, brze komande bez LLM-a, dashboard
6. Drugi backendi (Codex, API)

**Wake word:** mali engleski Vosk model sa slobodnim prepoznavanjem, ne sa listom reči. Ograničen rečnik se lažno palio na 53 od 72 srpske rečenice.

- **Na sintetičkom srpskom:** 13 od 16 "Hej Tesla" fraza prepoznato, 0 lažnih okidanja na 40 rečenica bez imena. Među tim rečenicama su bile i "testo", "Tešla", "vesla" i "električni auto".
- **Potrošnja:** oko 2% jednog jezgra.
