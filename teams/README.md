# Microsoft Teams app package

A Teams app package is a zip holding `manifest.json`, `color.png` and `outline.png`
at its root. This folder carries a manifest template, the icons and
`build-package.sh`, which fills in the template from your stack and zips the three
files.

| File | What it is |
|---|---|
| `manifest.template.json` | manifest schema 1.19, one bot, personal scope, `supportsFiles: true`, the bot's commands, the Browser-Use help tab |
| `build-package.sh` | fills in the placeholders and writes `manifest.json` and `nlsql-ai-employee-teams.zip` |
| `color.png` | 192 x 192 colour icon, the NLSQL bot avatar |
| `outline.png` | 32 x 32 outline icon, white on transparent |

## Placeholders

| Placeholder | Replaced with |
|---|---|
| `<MicrosoftAppId>` | the stack's `MicrosoftAppId` parameter: the Azure Bot's application (client) ID. Used for both `id` and `bots[0].botId` |
| `<CloudFrontDomain>` | the stack's `CloudFrontDomain` output, for example `d111111abcdef8.cloudfront.net`. It is the only entry in `validDomains`, which the live browser's Teams dialog needs, and the host of the Browser-Use tab |

## Building the package

In AWS CloudShell, in the Region of the stack, from a clone of this repository:

```shell
teams/build-package.sh --stack <stack-name>
```

It reads the stack once (`aws cloudformation describe-stacks`): the
`MicrosoftAppId` parameter, the `CloudFrontDomain` output and, when GeminiFeatures
is Enabled, the `BrowserViewUrl` output. It needs bash, jq or python3, and zip or
python3. `--region <region>` reads a stack in another Region.

Without AWS access, give the values yourself; the script then makes no AWS call:

```shell
teams/build-package.sh --app-id <MicrosoftAppId> --domain <CloudFrontDomain> [--browser-view]
```

`--browser-view` adds the Browser-Use help tab; give it only when the stack has
GeminiFeatures Enabled. The app ID can also come from the env var
`connections__serviceConnection__settings__clientId` or `MicrosoftAppId`.
`--version x.y.z` sets the app's version, and `--out-dir <dir>` writes the files
elsewhere.

The script writes `teams/manifest.json` and `teams/nlsql-ai-employee-teams.zip`.
Both are gitignored: they carry a real bot ID.

## Why personal scope only

Teams delivers files to a bot only in personal (one-to-one) chats, and
`supportsFiles` only takes effect there. Each user's files, memory, scheduled
tasks and Microsoft 365 sign-in are their own, which is a one-to-one
conversation too. Adding `team` or `groupChat` to `scopes` is not supported.

## Commands

The bot's command menu lists `/help`, `login`, `refresh`, `/schedule`, `/tasks`,
`/memory`, `/briefing` and `/watch`, with the same wording as the bot's `/help`.
`/schedule` keeps its slash, because the bot treats `schedule` followed by free
text as an ordinary request. Code mode's commands are not listed: this version
has no coder service.

## The Browser-Use help tab

With GeminiFeatures Enabled the app has a second personal tab, **Browser-Use**
(`entityId` `browser-use`), on `https://<CloudFrontDomain>/vnc/`, after the
bot's chat (`entityId` `conversations`). It is a help page: the live browser
opens in a Teams dialog from the **Open live browser** button on the bot's
cards, and the tab says so. Without Gemini there is no live browser, so
`build-package.sh` leaves `staticTabs` out and the chat is the only tab.

## Icon requirements

Microsoft validates both icons when the package is uploaded:

- **`color.png`**: PNG, exactly 192 x 192 pixels, full colour. Keep the symbol
  inside the central 120 x 120 pixels (36 pixels of padding on each side); the
  background may be a solid colour or transparent.
- **`outline.png`**: PNG, exactly 32 x 32 pixels, white (`#FFFFFF`) on a fully
  transparent background, no other colours. Teams tints it, so any colour other
  than white is rejected or renders badly. No padding is needed.
- Both files are named in `icons` in the manifest and sit at the root of the zip.
- `accentColor` (`#438AFF`, the avatar's blue) is the background Teams shows
  behind the outline icon.

Both icons are made from the NLSQL bot avatar that the image serves for its
cards: `static/img/bot-avatar.png` in the application repository (branch
`aws-marketplace-offer`), 280 x 289, sha256
`e299720a6d82998b0781ba74544bc0c799af0b90d1ea86b79e27cc4c7b2ed3fa`. `color.png` is
the avatar in the central 120 x 120 on a transparent background; `outline.png`
is the speech bubble in white with the robot cut out. To make them again
(Python 3 with Pillow 13.0.0, `pip install pillow==13.0.0`), from the repository
root:

```shell
python3 - <path to bot-avatar.png> teams <<'EOF'
import sys
from PIL import Image, ImageChops
avatar, out = sys.argv[1], sys.argv[2]
a = Image.open(avatar).convert("RGBA")
side = max(a.size)  # 280 x 289: centre it on a transparent square first
sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
sq.paste(a, ((side - a.width) // 2, (side - a.height) // 2))
# color.png: the avatar in the central 120 x 120 of a transparent 192 x 192
color = Image.new("RGBA", (192, 192), (0, 0, 0, 0))
color.alpha_composite(sq.resize((120, 120), Image.LANCZOS), (36, 36))
color.save(f"{out}/color.png", optimize=True)
# outline.png: the bubble in white, the robot (white in the avatar, where the
# bubble's blue has red 67) cut out, on a transparent 32 x 32
red, _, _, alpha = sq.split()
bubble = Image.eval(red, lambda v: 255 - max(0, min(255, (v - 67) * 255 // 188)))
outline = Image.new("RGBA", (32, 32), (255, 255, 255, 0))
outline.putalpha(ImageChops.multiply(alpha, bubble).resize((32, 32), Image.LANCZOS))
outline.save(f"{out}/outline.png", optimize=True)
EOF
```

With the Pillow 13.0.0 wheel from PyPI this reproduces the committed files byte
for byte (sha256
`15b998db7cf5d6af03504e2931cf7aad40db7319019ead071a431c2c42b7f96a` for
`color.png`, `4fb18b9e08caa9609510a960a58c0571d518179fdd1cb02fceb75c94fa523555`
for `outline.png`). To use other artwork, keep the same file names and sizes.

## Installing

The tenant must allow custom apps. Either:

- **For yourself:** Teams, **Apps**, **Manage your apps**, **Upload an app**,
  **Upload a custom app**, then choose the zip. This needs the *Upload custom apps*
  setting enabled for you in the Teams admin center (**Teams apps**, **Setup
  policies**).
- **For the organisation:** a Teams administrator uploads the zip in the Teams
  admin center under **Teams apps**, **Manage apps**, **Upload new app**, and
  makes it available to users.

## Rebuilding

Build the package again and upload it as an update when:

- the CloudFront domain changes (the stack was deleted and recreated);
- GeminiFeatures was switched on or off, which adds or removes the Browser-Use tab.

Keep `version` increasing when you re-upload, for example
`teams/build-package.sh --stack <stack-name> --version 1.0.1`.
