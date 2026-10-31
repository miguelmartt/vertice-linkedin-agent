# vertice-linkedin-agent

[Leer en español](README.es.md)

A LinkedIn agent that runs on your VPS as a systemd service. It finds AI and developer news, drafts a post with Claude Code and sends it to you on Telegram. It only publishes to your profile when you press "Publish".

I built it for the [VérticeDev](https://verticedev.es) LinkedIn profile and I'm sharing it as is, under the MIT license. Its messages and posts are in Spanish.

- **Finds** news every three hours in five places: trending models on Hugging Face, new GitHub repos gaining stars fast, the Hacker News front page, AI lab blogs (OpenAI, Google DeepMind, Google AI, Hugging Face and Mistral) and your own open source repos.
- **Drafts** on Monday, Wednesday and Friday at 08:30 with Claude Code and your subscription, not the API. Claude picks a topic from the best of each kind, reads the page or the README and writes the post without hype or invented numbers.
- **Promotes your repos** without overdoing it: at most one post a week about one of your repos. New releases come first, and `/repos` lets you pick one whenever you want.
- **Asks you** on Telegram with four buttons: Publish, Changes, Another topic and Discard. Under "Changes" you can say what to change or paste the corrected text.
- **Publishes** with LinkedIn's official Posts API. The post carries a link card (title, description and image), and the text is escaped so LinkedIn doesn't cut it.

Python standard library only (3.9+), SQLite and Telegram long polling: no dependencies and no open ports.

## How it works

```
every 3 h ──── collect ─ Hugging Face · GitHub · Hacker News · AI blogs · your repos → scored topics
Mon, Wed, Fri ─ draft ─ shortlist (or one of your repos) → Claude picks and writes → Telegram
               [Publish] [Changes] [Another topic] [Discard]
Publish ────── card image → Posts API → link to the post
daily ──────── LinkedIn permission reminders (7 and 2 days before) and old topic cleanup
```

## Security

- **Nothing is published without you.** Each "Publish" button is tied to the exact text it was shown with. A double click doesn't publish twice.
- **Only you** can use the bot: your user, in your private chat with it.
- **Everything from outside is untrusted.** Pages, READMEs and feeds are read as data, trimmed and never executed. URLs from Hacker News or a feed are only fetched if they resolve to a public IP, after every redirect too.
- **Claude has no tools**: no file, network or command access, in an empty folder and with only its own token.
- **Secrets stay out.** The LinkedIn code you paste is deleted from the chat. Tokens are masked in logs and errors.
- **Sandboxed systemd service**: its own user, and it only writes to its state folder.

## Setup

1. **Telegram bot.** Create a new one with @BotFather. Don't reuse another agent's bot: only one process can read a bot's messages.
2. **Claude Code.** `claude setup-token` gives you `CLAUDE_CODE_OAUTH_TOKEN`.
3. **LinkedIn app.** Create one at [LinkedIn Developers](https://www.linkedin.com/developers/apps) and add the "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect" products. Then add a redirect URL: any https page of yours works, even one that returns 404, because you'll only copy its address.
4. **On the VPS, as root:**

   ```bash
   git clone https://github.com/miguelmartt/vertice-linkedin-agent.git /root/vertice-linkedin-agent
   bash /root/vertice-linkedin-agent/deploy/install.sh
   runuser -u vla -- env HOME=/var/lib/vertice-linkedin-agent bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
   nano /etc/vertice-linkedin-agent/env
   bash /root/vertice-linkedin-agent/deploy/install.sh
   ```

5. **On Telegram:** send `/linkedin`, approve, then paste back the address LinkedIn redirected you to. Then send `/borrador`.

The LinkedIn permission lasts 60 days, and LinkedIn doesn't give these apps automatic renewal. The agent reminds you 7 and 2 days before it expires.

All settings are documented in [`deploy/env.example`](deploy/env.example).

## Tests

```bash
python -m unittest discover -s tests -t .
```

## License

[MIT](LICENSE) © 2026 VerticeDev
