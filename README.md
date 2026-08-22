# pwnkw_ha

Kenneth's **Home Assistant projects** — self-contained, copy-pasteable add-ons for HA. Each
one lives under [`projects/`](projects/) with its own README and install steps, so you can grab
just the piece you want.

## Projects

| Project | What it does |
|---|---|
| [**Discogs → Sonos Jukebox**](projects/discogs-jukebox/)<br><a class="source" href="https://github.com/kwilson9/pwnkw_ha/tree/main/projects/discogs-jukebox">Source on GitHub &#8599;</a> | Shows a random record from your Discogs collection on a dashboard tile — tap to play the album on Sonos (via Apple Music), double-tap to shuffle, hold to open Discogs, plus a "Play Artist Mix" button. |
| [**Mailbox Item Tracker**](projects/mailbox-tracker/)<br><a class="source" href="https://github.com/kwilson9/pwnkw_ha/tree/main/projects/mailbox-tracker">Source on GitHub &#8599;</a> | Live count of uncollected items in your remote/CMRA mailbox (e.g. Anytime Mailbox), counted in real time from the provider's "new mail" emails via IMAP, and auto-reset to zero when you arrive to collect. |
| [**Nanoleaf Room Clock**](projects/nanoleaf-clock-bar/)<br><a class="source" href="https://github.com/kwilson9/pwnkw_ha/tree/main/projects/nanoleaf-clock-bar">Source on GitHub &#8599;</a> | Turns a Nanoleaf Lines bar into a linear clock — minutes fill left to right, with an outdoor-temperature gauge, an overnight heartbeat, and a meeting countdown that drains the bar to empty exactly at meeting time. AppDaemon app driving the device over its local REST API. |

*More on the way.*

## Conventions

Every project in this repo follows the same rules, so they're predictable to install:

- **Self-contained.** Each project is a folder under `projects/<name>/` with its own `README.md`
  plus whatever that project actually needs. Config-based projects carry `sensors/`, `rest/`,
  `scripts.yaml`, `dashboard/` and a `configuration.example.yaml` showing the `!include` lines
  to add; standalone apps (e.g. AppDaemon) ship their code and their own example config instead.
- **Documented.** Each project's README covers requirements, install steps, and how it works.

## Using a project

1. Open the project folder and read its `README.md`.
2. Add any required secrets to your `secrets.yaml` (see [`secrets.yaml.example`](secrets.yaml.example)).
3. Copy the project's files into place — for config-based projects that means merging
   `scripts.yaml` and adding the `!include` lines from `configuration.example.yaml`; for
   standalone apps, follow the install steps in that project's README.
4. Restart / reload as the project's README says, then add its dashboard cards if it has any.

## Adding a new project

Start from the scaffold in
[`projects/_template/`](https://github.com/kwilson9/pwnkw_ha/tree/main/projects/_template)
(browsable on GitHub only — Jekyll excludes leading-underscore directories from this site):

1. Copy `projects/_template/` to `projects/<your-project>/`.
2. Delete the `.gitkeep` files and any folders/stubs the project doesn't use.
3. Fill in the project's `README.md`.
4. List it in both indexes: the **Projects** table above, and `projects/README.md`
   (add its install manifest there — runtime and file shape).
5. Append any secrets to [`secrets.yaml.example`](secrets.yaml.example) under a new heading.

## Repo layout

```
pwnkw_ha/
├── README.md              # this index
├── LICENSE                # MIT
├── CNAME                  # custom domain for the published site
├── .gitignore             # excludes secrets.yaml, .storage, logs, db
├── .github/FUNDING.yml    # Sponsor button
├── _config.yml            # Jekyll config for the published site
├── _layouts/default.html  # page shell for ha.206yall.com
├── assets/css/site.css    # site styling
├── secrets.yaml.example   # aggregated secret placeholders, grouped by project
└── projects/
    ├── README.md          # project index
    ├── _template/         # copy this to start a new project
    ├── discogs-jukebox/   # Discogs → Sonos random-record tile
    │   ├── README.md
    │   ├── configuration.example.yaml
    │   └── sensors/  rest/  scripts.yaml  dashboard/
    ├── mailbox-tracker/   # IMAP-driven mailbox item counter
    │   ├── README.md
    │   ├── configuration.example.yaml
    │   └── packages/  dashboard/
    └── nanoleaf-clock-bar/  # AppDaemon linear clock on Nanoleaf Lines
        ├── README.md
        ├── nanoleaf_clock_bar.py
        ├── appdaemon.example.yaml  apps.yaml.example
        └── tools/
```

## Support

These projects are free and always will be. If one of them saved you an evening,
you can [buy me a beer](https://buymeacoffee.com/bbzpt4y45mz) 🍺

[![Buy me a beer](assets/img/buy-me-a-beer.svg)](https://buymeacoffee.com/bbzpt4y45mz)

## License

[MIT](LICENSE) — do what you like, no warranty. Trademarks (Discogs, Apple Music, Sonos, Anytime Mailbox, Fastmail, Nanoleaf) belong
to their respective owners; this repo is not affiliated with or endorsed by any of them.
