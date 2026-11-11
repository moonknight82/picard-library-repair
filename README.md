# Library Repair for MusicBrainz Picard 3

Library Repair audits music folders for missing artist, album artist, album, title, track and disc tags, missing embedded front art, missing album-folder art, and `CD1` / `Disc 1` style subfolders. Healthy albums stay out of the results. After Picard saves a track, Library Repair also creates a missing `cover.jpg` from available front art, including art already embedded in that track when MusicBrainz supplied no new image. Existing covers are never overwritten, and audio is not rewritten by this cover step.

## Install from GitHub

In **Picard 3**, open **Options → Plugins → Install Plugin… → URL** and enter this repository URL:

```text
https://github.com/moonknight82/picard-library-repair
```

Install and enable **Library Repair**, then use **Tools → Library Repair → Audit music folders…**. The plugin uses Picard's bundled Mutagen and PyQt6; no API key is needed.

## Workflow

1. Scan a music folder. The list separates disc-folder issues from metadata and artwork issues.
2. Choose **Select all disc-folder albums**, then **Review consolidations…**. The review window shows each album's moves and conflicts. Only ready albums are checked for application; you can uncheck any of them.
3. Apply the checked albums. Library Repair rescans those album folders and removes only albums with no remaining problems. Conflicts and tag/artwork problems stay visible.
4. Choose **Select tag/artwork albums**, then **Add selected as Picard clusters**. Each exact album folder gets its own cluster in Picard's left panel, even if another artist has an album with the same title. This action does not start an automatic lookup or scan; choose Picard's **Lookup** or **Scan** yourself for each cluster. You can run a full audit again after changes made outside the plugin.

If a storage I/O error stops a batch, Library Repair leaves unfinished source files in place and refreshes only the albums it reached. Review a new plan before resuming; completed tracks already in the album root are not moved again. On removable media, check the card connection and filesystem before retrying a timeout.

The automatic `cover.jpg` step applies to tracks saved **after** this plugin version is loaded. Previously saved folders can be loaded and saved again in Picard. Albums with no usable front art remain flagged for review.

The plugin's reviewed consolidation moves tracks from disc subfolders into the album root as `1.01 Song.flac`, `2.01 Song.flac`, and so on. It retains separate numeric disc and track tags, never overwrites an existing destination, and removes empty disc folders only after successful moves. Review the preview and conflicts before applying changes to your library.

For files whose disc and track tags are already correct, macOS uses an exclusive same-volume rename when the destination filesystem supports it. This avoids rewriting the audio. Files needing tag edits, and volumes without exclusive rename support, use the original exclusive-create, verified-copy path. The FAT32 SD volume tested with this project did **not** support exclusive rename; it therefore still needs the copy path. The plugin does not silently switch to a normal rename that could overwrite a destination.

Picard 3's Git plugin installer can check this repository for updates. Device playback and album grouping should still be checked on your player after a sample album is repaired.

Licensed under GPL-2.0-or-later. See [COPYING.txt](COPYING.txt).
