PNGs in this directory are 64x64 Twitter-style emoji images from the
`emoji-datasource-twitter` npm package (https://github.com/iamcal/emoji-data),
MIT licensed. They were fetched once at build time (via the npm registry,
which this project's build environment can reach even where arbitrary
websites are blocked) and are bundled directly into the cog rather than
fetched from any CDN at drop-time -- same "download once, store locally"
rule this project applies to AniList character art (see storage.py / the
design doc's photodrop-derived lesson).

Filenames are the emoji's Unicode codepoint(s) in lowercase hex, joined with
`-` (e.g. `2600-fe0f.png` for ☀️), matching emoji-datasource's own `unified`
field. This set covers exactly the 50 emoji in constants.EMOJI_POOL -- if
EMOJI_POOL is ever extended, re-run the same lookup against a fresh copy of
`emoji-datasource-twitter`'s emoji.json to add the new ones; there is no
runtime fallback that fetches a missing emoji automatically, by design.
