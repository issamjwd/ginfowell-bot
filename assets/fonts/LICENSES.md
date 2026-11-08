# Fonts

All fonts are under the SIL Open Font License 1.1 (`OFL-1.1.txt`). They are distributed unmodified.

| File | Font | Copyright | Where it came from |
|---|---|---|---|
| `Montserrat-Bold.ttf`, `Montserrat-Medium.ttf` (shipped) | Montserrat 8.000 | Copyright 2011 The Montserrat Project Authors (https://github.com/JulietaUla/Montserrat) | the OFL font files bundled with Roblox Studio (byte-identical upstream builds, name table checked) |
| `Merriweather-Regular.ttf`, `Merriweather-Italic.ttf` (shipped) | Merriweather 2.002 | Copyright 2016 The Merriweather Project Authors (https://github.com/EbenSorkin/Merriweather), Reserved Font Name "Merriweather" | same |
| `EBGaramond.ttf`, `EBGaramond-Italic.ttf` (optional, fetched in CI, not committed) | EB Garamond | Copyright 2017 The EB Garamond Project Authors | google/fonts `ofl/ebgaramond/` at the commit + sha256 pinned in `fonts.lock.json` |

Shipped files are hash-checked by `tests/test_fonts.py` (hashes in `bot/fonts.py` `SHIPPED`).
