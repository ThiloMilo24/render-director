# `data/reference_library/`

Local reference photo library for the tier-0 retrieval step
(`render_director.rag.library`). Contents are gitignored: real project
photos are large and usually come with image rights you cannot publish.

One sub-folder per project; the file name carries the metadata:

```
<project>_<nnn>[_<region>]_<type>_<material>_<time>_<angle..>_<distance>.jpg
projecta_005_hotel_woodglass_day_corner_close.jpg
```

- `type`: building type, see `BUILDING_TYPE_VOCAB` (`hotel`, `residential`, ...)
- `material`: compound of `wood`, `glass`, `metal`, `plaster`, `stone`,
  `green`, `concrete` (e.g. `woodglass`)
- `time`: `day`, `dusk`, `night`, `overcast`, `dawn`
- `distance`: `close`, `medium`, `far`

An optional `<same name>.txt` next to a photo is read as caption.
Point `RENDER_REFERENCE_LIBRARY_ROOT` elsewhere to use another folder.
