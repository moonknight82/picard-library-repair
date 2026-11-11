# Library Repair for MusicBrainz Picard 3

Library Repair audits music folders for missing artist, album artist, album, title, track and disc tags, missing embedded front art, missing album-folder art, and `CD1` / `Disc 1` style subfolders. Healthy albums stay out of the results. You can open selected albums in Picard for MusicBrainz matching and review a multi-disc consolidation before applying it.

## Install from GitHub

In **Picard 3**, open **Options → Plugins → Install Plugin… → URL** and enter this repository URL:

```text
https://github.com/moonknight82/picard-library-repair
```

Install and enable **Library Repair**, then use **Tools → Library Repair → Audit music folders…**. The plugin uses Picard's bundled Mutagen and PyQt6; no API key is needed.

The plugin's reviewed consolidation moves tracks from disc subfolders into the album root as `1.01 Song.flac`, `2.01 Song.flac`, and so on. It retains separate numeric disc and track tags, never overwrites an existing destination, and removes empty disc folders only after successful moves. Review the preview and conflicts before applying changes to your library.

Picard 3's Git plugin installer can check this repository for updates. Device playback and album grouping should still be checked on your player after a sample album is repaired.

Licensed under GPL-2.0-or-later. See [COPYING.txt](COPYING.txt).
