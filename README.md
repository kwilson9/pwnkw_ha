# pwnkw_ha

Kenneth's **Home Assistant projects** — self-contained, copy-pasteable add-ons for HA. Each
one lives under [`projects/`](projects/) with its own README and install steps, so you can grab
just the piece you want.

## Projects

| Project | What it does |
|---|---|
| [**Discogs → Sonos Jukebox**](projects/discogs-jukebox/) | Shows a random record from your Discogs collection on a dashboard tile — tap to play the album on Sonos (via Apple Music), double-tap to shuffle, hold to open Discogs, plus a "Play Artist Mix" button. |

*More on the way.*

## Conventions

Every project in this repo follows the same rules, so they're predictable to install and safe to publish:

- **Self-contained.** Each project is a folder under `projects/<name>/` with its own `README.md`
  and, where relevant, `sensors/`, `rest/`, `scripts.yaml`, `dashboard/`, and a
  `configuration.example.yaml` snippet showing any `!include` lines to add.
- **No secrets in git.** All credentials go through Home Assistant's `secrets.yaml` (git-ignored)
  referenced via `!secret`. See [`secrets.yaml.example`](secrets.yaml.example) for the values each
  project needs.
- **Documented.** Each project's README covers requirements, install steps, and how it works.

## Using a project

1. Open the project folder and read its `README.md`.
2. Add any required secrets to your `secrets.yaml` (see [`secrets.yaml.example`](secrets.yaml.example)).
3. Copy the project's files into your HA config — merge its `scripts.yaml`, add the `!include`
   lines from its `configuration.example.yaml`.
4. Restart / reload as the project's README says, then add its dashboard cards.

## Adding a new project

Start from the scaffold in [`projects/_template/`](projects/_template/):

1. Copy `projects/_template/` to `projects/<your-project>/`.
2. Delete the `.gitkeep` files and any folders/stubs the project doesn't use.
3. Fill in the project's `README.md`.
4. Add a row to the **Projects** table above.
5. Append any secrets to [`secrets.yaml.example`](secrets.yaml.example) under a new heading.

## Repo layout

```
pwnkw_ha/
├── README.md              # this index
├── LICENSE                # MIT
├── .gitignore             # excludes secrets.yaml, .storage, logs, db
├── secrets.yaml.example   # aggregated secret placeholders, grouped by project
└── projects/
    ├── _template/         # copy this to start a new project
    └── discogs-jukebox/   # first project
        ├── README.md
        ├── configuration.example.yaml
        ├── sensors/  rest/  scripts.yaml  dashboard/
```

## License

[MIT](LICENSE) — do what you like, no warranty. Trademarks (Discogs, Apple Music, Sonos) belong
to their respective owners; this repo is not affiliated with or endorsed by any of them.
