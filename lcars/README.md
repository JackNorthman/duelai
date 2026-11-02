# lcars/ - bring your own LCARS theme

The console in `ui/index.html` is built on the **LCARS Classic** theme by
Jim Robertus, [www.TheLCARS.com](https://www.thelcars.com). That theme is
not part of this repository and is not covered by its MIT licence. Download
it from TheLCARS.com and follow its terms (credit to TheLCARS.com is
required).

`server.py` serves this folder at `/lcars/`. The UI expects these files:

```
lcars/css/classic.css          the Classic theme stylesheet (built against v24.2)
lcars/css/Antonio-*.woff(2)    the theme's Antonio webfont, if your copy of
                               classic.css loads it locally
lcars/js/lcars.js              the theme's small helper script
lcars/audio/beep1.mp3 .. beep4.mp3   the theme's button beeps (optional)
```

Without them the console still works, but it is unstyled and silent. The
layout CSS inside `ui/index.html` is DUELAI's own and only uses the theme's
class names and colour variables (`--orange`, `--bluey`, `--african-violet`
and so on).
