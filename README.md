# pwnkw_ha

Kenneth's **Home Assistant projects** — self-contained, copy-pasteable add-ons for HA. Each
one lives under [`projects/`](projects/) with its own README and install steps, so you can grab
just the piece you want.

## Projects

| Project | What it does |
|---|---|
| [**Discogs → Sonos Jukebox**](projects/discogs-jukebox/) | Shows a random record from your Discogs collection on a dashboard tile — tap to play the album on Sonos (via Apple Music), double-tap to shuffle, hold to open Discogs, plus a "Play Artist Mix" button. |
| [**Mailbox Item Tracker**](projects/mailbox-tracker/) | Live count of uncollected items in your remote/CMRA mailbox (e.g. Anytime Mailbox), counted in real time from the provider's "new mail" emails via IMAP, and auto-reset to zero when you arrive to collect. |
| [**Nanoleaf Room Clock**](projects/nanoleaf-clock-bar/) | Turns a Nanoleaf Lines bar into a linear clock — minutes fill left to right, with an outdoor-temperature gauge, an overnight heartbeat, and a meeting countdown that drains the bar to empty exactly at meeting time. AppDaemon app driving the device over its local REST API. |

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
    ├── discogs-jukebox/   # first project
    │   ├── README.md
    │   ├── configuration.example.yaml
    │   └── sensors/  rest/  scripts.yaml  dashboard/
    └── mailbox-tracker/   # IMAP-driven mailbox item counter
        ├── README.md
        ├── configuration.example.yaml
        └── packages/  dashboard/
```

## Support

These projects are free and always will be. If one of them saved you an evening,
you can [buy me a beer](https://buymeacoffee.com/bbzpt4y45mz) 🍺

<!-- Two buttons below; exactly one renders on each surface.
     - github.com strips <style> and <script> from markdown -> the static image below shows.
     - The Pages site (Jekyll) keeps both -> the CSS hides the image and the script draws the live button.
     Editing note: the slug bbzpt4y45mz appears in three places (here x2, and .github/FUNDING.yml). -->
<style>.bmc-static { display: none; }</style>

<p class="bmc-static">
  <a href="https://buymeacoffee.com/bbzpt4y45mz"><img alt="Buy me a beer"
     src="https://img.buymeacoffee.com/button-api/?text=Buy%20me%20a%20beer&amp;emoji=%F0%9F%8D%BA&amp;slug=bbzpt4y45mz&amp;button_colour=40DCA5&amp;font_colour=ffffff&amp;font_family=Bree&amp;outline_colour=000000&amp;coffee_colour=FFDD00"></a>
</p>

<script type="text/javascript" src="https://cdnjs.buymeacoffee.com/1.0.0/button.prod.min.js" data-name="bmc-button" data-slug="bbzpt4y45mz" data-color="#40DCA5" data-emoji="🍺"  data-font="Bree" data-text="Buy me a beer" data-outline-color="#000000" data-font-color="#ffffff" data-coffee-color="#FFDD00" ></script>

## License

[MIT](LICENSE) — do what you like, no warranty. Trademarks (Discogs, Apple Music, Sonos) belong
to their respective owners; this repo is not affiliated with or endorsed by any of them.
