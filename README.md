# Tesla

Glasovni asistent za GNOME (Wayland). Pritisneš prečicu ili kažeš "Hej Tesla", kažeš šta ti treba, a Tesla odgovori glasom. Usput može da otvori aplikaciju, sajt, terminal ili VS Code, da pronađe nešto u tvojim projektima ili da pogleda šta ti je na ekranu.

| Deo | Šta koristi | Gde radi |
|---|---|---|
| Buđenje ("Hej Tesla") | Vosk, mali engleski model | lokalno, CPU |
| Kraj rečenice (VAD) | Silero VAD | lokalno, CPU |
| Prepoznavanje govora | Groq `whisper-large-v3`; rezerva: whisper.cpp `large-v3-turbo` | Groq cloud; rezerva lokalno na iGPU (Vulkan) |
| Mozak | Claude Code (Agent SDK), Haiku 5.5, preko tvoje Claude subskripcije | Anthropic cloud |
| Glas | Microsoft neural glasovi `sr-RS` (edge-tts) | Microsoft cloud |

## Instalacija

```bash
scripts/install.sh
```

Skripta instalira:

- Teslu kao zaseban program (`uv tool`, u `~/.local/bin/tesla`);
- notch ekstenziju za GNOME Shell;
- prečicu `Super+Alt+Space`;
- systemd korisnički servis, koji pokreće Teslu pri svakoj prijavi i sam je podigne ako padne.

Prvi put traje nekoliko minuta, jer pravi i whisper.cpp sa Vulkanom za iGPU, bez `sudo`. Ako u repou postoji `.env`, kopira ga u `~/.config/tesla/env` (dozvole 600), odakle servis čita tajne, npr. `GROQ_API_KEY`.

- **Nova verzija:** posle izmena u repou pokreni `scripts/install.sh` ponovo. Zameniće verziju koja radi i restartovati servis. Rad u repou ne dira Teslu koja radi, sve dok ponovo ne instaliraš. Ako se promenila ekstenzija, skripta kaže da se odjaviš i ponovo prijaviš, jer GNOME na Waylandu učitava ekstenzije samo pri prijavi.
- **Komande:** `tesla version`, `journalctl --user -u tesla -f` za logove, `systemctl --user restart tesla`.
- **Uklanjanje:** `scripts/uninstall.sh`. Konfiguracija i modeli ostaju.

### Razvoj

- **Pokretanje iz repoa:** prvo zaustavi servis (`systemctl --user stop tesla`), pa pokreni `uv run tesla`. Dve Tesle ne mogu da rade istovremeno: druga se odmah ugasi sa porukom da Tesla već radi.
- **Posle `uv sync`, `uv add` ili `uv remove`:** te komande iz razvojnog venv-a uklanjaju Vulkan whisper.cpp, pa pokreni `scripts/build-whispercpp-vulkan.sh --install`. Instalirana Tesla to ne oseti.
- **Razvoj ekstenzije:** `scripts/nested-shell.sh` pokreće GNOME u prozoru, sa ekstenzijom iz repoa i izolovanim podešavanjima. Instalirana kopija ostaje netaknuta.

## Korišćenje

**Prečica `Super+Alt+Space`:**

- **Kratak pritisak:** Tesla sluša dok ne ućutiš.
- **Pritisak pa držanje `Super+Alt`:** sluša dok ne pustiš, i pauze joj tada ne smetaju.
- **Ponovni pritisak dok slušaš:** odmah završava snimanje.
- **Ponovni pritisak dok priča ili razmišlja:** prekida je i ponovo sluša.

**"Hej Tesla":** kaži ime i odmah nastavi ("Hej Tesla, otvori terminal"), ili napravi pauzu pa kaži zahtev.

- Isključuješ ga glasom ("isključi buđenje") ili sa `tesla wake off`, a ponovo uključuješ sa `tesla wake on`. Trajno: `[wake] enabled = false`.
- Dok je uključeno, mikrofon je stalno otvoren, pa GNOME stalno prikazuje indikator mikrofona. Zvuk se obrađuje lokalno i ne šalje se nigde dok Tesla ne čuje ime.

**Posle odgovora** Tesla još nekoliko sekundi čeka nastavak bez prečice (`[listen] followup_s`). Kad se oprostiš, odmah prestaje da sluša.

**Kucani zahtev:** `tesla send "Otvori Firefox"` šalje pitanje Tesli koja radi; odgovor stiže u notch.

### Glasovne komande koje ne idu do modela

| Kažeš | Šta se desi |
|---|---|
| "nova tema" / "počnimo ispočetka" | nova konverzacija |
| "pređi na sonet" / "pređi na haiku" / "pređi na opus" | promena modela, kontekst ostaje |
| "stani" / "nema veze" | tih prekid |
| "hvala, ćao" / "to je sve" / "laku noć" | kratak pozdrav, pa mirovanje i skrivanje notch-a |
| "otvori drugi link" / "otvori sve izvore" | otvara izvore iz poslednjeg odgovora |
| "isključi buđenje" / "uključi buđenje" | "Hej Tesla" isključeno ili uključeno |

Samo "ćao" na početku razgovora je pozdrav, ne kraj. Oproštaj koji ove fraze ne pokrivaju prepozna model: na kraj odgovora doda skrivenu oznaku `[[kraj]]`, koja gasi slušanje.

### Notch

Notch iskače ispod sata, uvek na glavnom monitoru:

- **Slušanje:** prikazuje nivo mikrofona.
- **Razmišljanje i rad alata:** prikazuje šta Tesla radi ("Čitam fajl…").
- **Odgovor:** tekst rečenice se pojavljuje tek kad ona krene da se izgovara, pa ne čitaš ispred Tesle. U tihom režimu (`[tts] enabled = false`) tekst se prikazuje odmah.
- **Kod, putanje i vrednosti:** dobijaju poseban blok sa dugmetom za kopiranje. Ne izgovaraju se.
- **Izvori:** prikazuju se kao numerisane kartice. Klik ih otvara u browseru, a otvaraju se i glasom.
- **Rizične radnje:** pojavljuje se kartica sa dugmićima "Dozvoli" i "Odbij".

Bez ekstenzije sve radi i dalje, ali preko notifikacija.

### Kontekst ekrana

Za svako obraćanje ekstenzija beleži aktivni prozor (naslov i aplikaciju), tekst koji si selektovao u poslednjih 90 s i snimak ekrana.

**Koji ekran se snima** (`[screen] capture`):

| Vrednost | Šta se snima |
|---|---|
| `pointer` (podrazumevano) | monitor na kom je kursor miša |
| `focused` | monitor aktivnog prozora |
| `primary` | glavni monitor |
| `all` | svi monitori u jednoj slici |

**Kada nastaje snimak:**

- **prečica:** u trenutku pritiska, pre nego što notch iskoči;
- **"Hej Tesla":** odmah posle buđenja;
- **nastavak razgovora:** čim završiš rečenicu, paralelno sa prepoznavanjem govora;
- **kucani zahtev:** pre slanja modelu.

Kad je notch već vidljiv, ne sakriva se, nego se njegovo mesto na snimku prekrije tamnim pravougaonikom.

Snimak ostaje u `$XDG_RUNTIME_DIR/tesla/` (tmpfs; svaki novi prepisuje stari). Modelu ide samo kad u rečenici pomeneš ekran ("šta je ovo", "ova greška", "na ekranu") ili kad model sam pozove alat `look_at_screen`.

## Bezbednost i privatnost

Svaki poziv alata prolazi kroz `src/tesla/policy.py`:

- **Slobodno:** čitanje i pretraga fajlova, web, Bash komande koje samo čitaju (`ls`, `cat`, `git status`, `git log`…) i Teslini alati za otvaranje aplikacija, sajtova, foldera i terminala.
- **Uz odobrenje** (kartica u notch-u, bez ekstenzije notifikacija): svaki upis ili izmena fajla, svaka druga Bash komanda, čitanje `.env` fajlova. Ako odbiješ, a model posle toga ćuti, Tesla kaže da radnja nije izvršena.
- **Nikad:** `sudo`, brisanje `/` ili `~`, `mkfs`, `dd` na disk, kao i bilo kakav pristup `~/.ssh`, `~/.gnupg`, keyring-u, cloud kredencijalima i kolačićima browsera.

**Claude Code** radi izolovano: bez tvojih korisničkih podešavanja, MCP servera i CLAUDE.md fajlova, sa sopstvenim workspace-om u `~/.local/share/tesla/workspace`. `ANTHROPIC_API_KEY` se namerno uklanja iz okruženja, da bi se uvek koristila subskripcija.

**Transkripti:** Claude Code čuva transkript svake sesije, uključujući priložene snimke ekrana, u `~/.claude/projects/-home-vuk--local-share-tesla-workspace/`. Tesla ga briše čim se konverzacija završi ("nova tema", 15 minuta neaktivnosti, gašenje daemona). Pri pokretanju briše i zaostale transkripte.

**Šta napušta računar:**

| Kome | Šta |
|---|---|
| Anthropic | tvoje poruke, tekstualni kontekst, snimci ekrana kad se priključe |
| Groq | snimak tvog glasa za svako obraćanje (sa `[stt] service = "local"` ostaje na računaru) |
| Microsoft | tekst onoga što Tesla izgovara |

## Konfiguracija

Konfiguracija je u `~/.config/tesla/config.toml`; svaki ključ je opcion. Pogledaj `config.example.toml`. Posle izmene: `systemctl --user restart tesla`.

**Prepoznavanje govora** (`[stt]`):

- **`service = "groq"`** (podrazumevano) traži `GROQ_API_KEY`. Lokalni model je uvek učitan kao rezerva i preuzima kad:
  - nema ključa;
  - Groq vrati grešku;
  - Groq ne odgovori za `groq_timeout_s`;
  - se potroši ograničenje (429). Tada Tesla do isteka vremena koje Groq zada (`retry-after`) ide pravo na lokalni model.
- **`service = "local"`:** samo lokalno.
- **`engine = "auto"`** bira lokalni model: whisper.cpp sa `large-v3-turbo` na iGPU, ako je instaliran Vulkan build, a inače faster-whisper `small` na CPU-u.

**Glas** (`[tts]`):

- `voice = "sophie"` za ženski glas; `"nicholas"` je podrazumevani.
- `rate = "+10%"` za brzinu.
- Preslušavanje: `tesla say --voice sophie "Zdravo"`.

**Ako Tesla prestane da priča** i u logu vidiš `403` od Microsofta, Microsoft je promenio proveru zahteva. Rešenje je novija `edge-tts`:

```bash
uv lock --upgrade-package edge-tts && uv sync
scripts/build-whispercpp-vulkan.sh --install
scripts/install.sh
```

## Arhitektura

```
Super+Alt+Space ─► ekstenzija (dev.vuk.TeslaShell): notch, kontekst, snimak ekrana, hold-to-talk
                     │  Activate(kontekst) / StopListening / Approve      ▲ Event(vrsta, vrednost)
                     ▼                                                    │
                   daemon (dev.vuk.Tesla, systemd --user) ────────────────┘
                           │
  "Hej Tesla" (Vosk) ─┐
  mikrofon ───────────┴─► Silero VAD ─► Groq Whisper (rezerva: whisper.cpp) ─► Claude Code (Agent SDK)
                                                         alati: Read/Glob/Grep/Bash/Web + mcp__tesla__*
  zvučnik ◄── Edge TTS, strimovano, rečenica po rečenicu ◄──────────────────┘  dozvole: policy.py
```

| Modul | Uloga |
|---|---|
| `daemon.py` | mašina stanja, D-Bus servis, tok jednog obraćanja, lokalne komande, usklađivanje teksta sa govorom |
| `brain.py` | jedan dugoživeći `claude` proces, prekid, promena modela, kraj konverzacije i brisanje transkripata |
| `stt.py` | Groq Whisper sa lokalnom rezervom (whisper.cpp ili faster-whisper), filter halucinacija |
| `tts.py` | Edge TTS: prebacivanje u ćirilicu, izgovor engleskih reči, strimovano dekodiranje |
| `audio.py` | deljeni mikrofon, zvučnik koji javlja kad koja rečenica stvarno počne, zvučni signali |
| `vad.py` | Silero VAD, okvir po okvir |
| `wake.py` | "Hej Tesla": Vosk detektor i skidanje imena sa početka transkripcije |
| `screen.py` | kontekst ekrana, maska preko notch-a, kada se šalje snimak, alat `look_at_screen` |
| `policy.py` | dozvole za alate |
| `desktop.py` | MCP alati: `open_app`, `open_url`, `open_path`, `open_terminal`, `open_in_vscode`, `notify` |
| `text.py` | latinica, deljenje na rečenice za govor, blokovi koda, izvori, oznaka kraja razgovora |
| `prompt.py` | sistemski prompt i spisak tvojih projekata |
| `gnome-extension/tesla@vuk.dev` | GNOME Shell 46 ekstenzija: notch, hvatanje konteksta, hold-to-talk |
| `scripts/install.sh`, `uninstall.sh` | instalacija kao servis i uklanjanje |
| `scripts/build-whispercpp-vulkan.sh` | whisper.cpp sa Vulkanom, bez `sudo` |
| `scripts/bench_*.py` | merenja modela, STT-a i glasa |

## Merenja

Sve na Ryzen 7 5700U (Radeon Vega iGPU), srpski.

**Mozak, kroz `claude` CLI sa toplim procesom (medijana):**

| Model | Prvi token | Prva rečenica |
|---|---|---|
| **Haiku 5.5, effort low (podrazumevano)** | **0,57 s** | **0,9 s** |
| Haiku 4.5, razmišljanje isključeno | 0,55–1,5 s | 1,0–1,5 s |
| Sonnet 5.5, effort low | 1,1–2,2 s | 2,1–2,7 s |
| Opus 5.5, effort low | 1,6 s | 2,1 s |

Haiku i dalje greši na zadacima kao što je brojanje fajlova; za to reci "pređi na sonet". Razmišljanje kod 5.5 modela ne može potpuno da se isključi.

**Prepoznavanje govora, Vukov glas, 6 rečenica:**

| | Vreme po rečenici | WER |
|---|---|---|
| **Groq `whisper-large-v3` (podrazumevano)** | **0,32 s** | **16%** |
| Groq `whisper-large-v3-turbo` | 0,24 s | 19% |
| whisper.cpp `large-v3-turbo` q8, iGPU, prozor ≥15 s (lokalna rezerva) | 1,5 s | 20–26% |
| whisper.cpp `large-v3-turbo` q8, iGPU, prozor 30 s | 3,4 s | 9% |
| whisper.cpp `medium` q5, iGPU, prozor 30 s | 2,3 s | 24% |
| whisper.cpp `small` q8, iGPU | 0,8 s | 43% |
| faster-whisper `small`, CPU (rezerva bez Vulkana) | 1,9 s | 37% |

- **Uži prozor ruši veće modele:** `turbo` sa prozorom ispod 15 s izbacuje besmislice ili engleski, a `medium` upada u petlje ponavljanja.
- **Bez GPU-a je `turbo` neupotrebljiv:** na CPU-u traje oko 11 s.
- **`small` modeli dodatno trenirani na srpskom** (Sagicc, istomin) dali su 30–34% grešaka.
- **Ostalo:** `tiny` i `base` su neupotrebljivi. Parakeet v3 ne podržava srpski. flash attention ne pomaže na Vulkanu.

**Glas:** Edge `sr-RS-NicholasNeural` i `sr-RS-SophieNeural`.

- **Kašnjenje:** prvi zvuk stiže za 0,7–1,3 s. Do Microsoftove promene u oktobru 2026. bilo je oko 0,5 s. Zvuk se pušta dok još stiže.
- **Ćirilica:** ovi glasovi latinicu čitaju kao besmislicu, pa se sve izgovara u ćirilici. Engleske reči prvo dobiju srpski izgovor ("Firefox" postaje "fajerfoks", "VS Code" "vi es kod").
- **Piper je izbačen:** lokalni `sr_RS-serbski_institut` imao je razumljivost 47–61% WER kad se vrati kroz STT, a Edge 26%, što je praktično donja granica tog merenja.

**"Hej Tesla":** Vosk sa slobodnim prepoznavanjem, ne sa listom reči, jer se ograničen rečnik lažno palio na 53 od 72 srpske rečenice.

- **Na sintetičkom srpskom:** 13 od 16 fraza prepoznato, 0 lažnih okidanja na 40 rečenica bez imena (među njima "testo", "Tešla", "vesla", "električni auto").
- **Potrošnja:** oko 2% jednog jezgra.

## Plan

Urađeno:

- glasovna petlja i notch;
- prečica sa držanjem tastera;
- odobravanje radnji;
- kontekst ekrana;
- "Hej Tesla";
- Groq i lokalni STT;
- Edge glas;
- izvori sa karticama;
- instalacija kao servis.

Sledeće:

1. Prvi git commit i `pytest` testovi za delove koji su se već kvarili.
2. Memorija: profil o tebi, istorija razgovora sa pretragom, dnevna refleksija.
3. Kucanje u aktivni prozor ("napiši Ivanu…") i diktiranje posebnom prečicom.
4. Delegiranje poslova Claude Code-u u pozadini, sa rezimeom kad završi.
5. Tajmeri i podsetnici.
6. Brze komande bez modela: terminal, muzika (MPRIS).
7. Drugi backendi (Codex, API).
