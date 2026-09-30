# Microsoft Teams app package

A Teams app package is a zip holding `manifest.json`, `color.png` and `outline.png`
at its root. This folder carries a manifest template and placeholder icons; the
buyer fills in two values and zips them. Step 4 of the
[deployment guide](../docs/aws-deployment-guide.md#step-4--install-the-bot-in-teams)
walks through it.

| File | What it is |
|---|---|
| `manifest.template.json` | manifest schema 1.19, one bot, personal scope, `supportsFiles: true` |
| `color.png` | 192 x 192 colour icon (placeholder) |
| `outline.png` | 32 x 32 outline icon (placeholder) |

## Placeholders

| Placeholder | Replace with |
|---|---|
| `<MicrosoftAppId>` | the stack's `MicrosoftAppId` parameter: the Azure Bot's application (client) ID. Used for both `id` and `bots[0].botId` |
| `<CloudFrontDomain>` | the host part of the stack's `MessagingEndpoint` output, for example `d111111abcdef8.cloudfront.net`. Chart and CSV links in answers point at it |

Build the package from the stack, with no values typed by hand:

```shell
STACK=<stack-name>; REGION=<region>
APP_ID=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Parameters[?ParameterKey=='MicrosoftAppId'].ParameterValue" --output text)
DOMAIN=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='MessagingEndpoint'].OutputValue" --output text | cut -d/ -f3)
sed -e "s/<MicrosoftAppId>/$APP_ID/g" -e "s/<CloudFrontDomain>/$DOMAIN/g" \
  manifest.template.json > manifest.json
zip -j nlsql-ai-agent-teams.zip manifest.json color.png outline.png
```

`manifest.json` and the zip are gitignored: they carry a real bot ID.

## Why personal scope only

Teams delivers files to a bot only in personal (one-to-one) chats, and
`supportsFiles` only takes effect there. Questions about an attached file are the
reason the scope is personal. Adding `team` or `groupChat` to `scopes` works for
text questions, but files sent there never reach the bot.

## Icon requirements

Microsoft validates both icons when the package is uploaded:

- **`color.png`**: PNG, exactly 192 x 192 pixels, full colour. Keep the symbol
  inside the central 120 x 120 pixels (36 pixels of padding on each side); the
  background may be a solid colour or transparent.
- **`outline.png`**: PNG, exactly 32 x 32 pixels, white (`#FFFFFF`) on a fully
  transparent background, no other colours. Teams tints it, so any colour other
  than white is rejected or renders badly. No padding is needed.
- Both files are named in `icons` in the manifest and sit at the root of the zip.
- `accentColor` (`#D85028`) is the background Teams shows behind the outline icon.

The icons here are generated placeholders (a white speech bubble on the accent
colour) that satisfy these rules. Replace them with the NLSQL brand mark before
distributing the app widely; keep the same file names and sizes.

## Installing

The tenant must allow custom apps. Either:

- **For yourself:** Teams, **Apps**, **Manage your apps**, **Upload an app**,
  **Upload a custom app**, then choose the zip. This needs the *Upload custom apps*
  setting enabled for you in the Teams admin center (**Teams apps**, **Setup
  policies**).
- **For the organisation:** a Teams administrator uploads the zip in the Teams
  admin center under **Teams apps**, **Manage apps**, **Upload new app**, and
  makes it available to users.

If the CloudFront domain changes (the stack was deleted and recreated), rebuild
the package with the new domain and upload it again as an update. Keep
`version` increasing (for example `1.0.1`) when you re-upload.
