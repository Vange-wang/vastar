# Vastar terminal startup artwork

`Vastar-wallpaper.png` is an unchanged copy of the user's supplied wallpaper.
`wallpaper.frames.zlib` is the compressed manifest; `wallpaper-NNN.frames.zlib`
contains the pre-rendered UTF-8 ANSI frames for that width. Only the selected
width's file is decoded at launch.
The source wallpaper is not opened at runtime. No Pillow dependency is required
by the launcher. Twelve widths (40 through 384 cells) each contain eight phases.
Selection follows terminal width; excess top/bottom sky is cropped to leave
space for the welcome text. Horizontal cropping preserves the entire wordmark.

The image is adapted to terminal cells. The case-sensitive `Vastar` lettering
is rebuilt separately by `ui/startup_logo.py`, using a blue/purple/rose/gold
palette. In the terminal derivative only, nearby textured sky replaces the
photographed lettering so there is one wordmark, and sky remains visible between
its strokes. Only background star points shimmer. The final frame stays in normal
terminal scrollback when loading finishes. The input loop owns the terminal
after the animation thread is stopped.

Offline rebuild, with a Python interpreter that has Pillow installed:

```powershell
python -X utf8 scripts/build_startup_art.py
```

The canonical `vastar.cmd` invokes `scripts/start_vastar.py`, which shows the
cache before importing the agent. Maintenance/help commands, redirected output,
`NO_COLOR`, and very small terminals retain the regular text startup.
