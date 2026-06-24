# Notion Webhook Server
A personal automation backend that reacts to changes in a Notion workspace, using AI to summarize new notes and keep task/calendar status blocks up to date, running continuously as a systemd service on a Raspberry Pi.


## Required Software & Hardware
Everything needed to stand up this project from scratch.

### Hardware
- Raspberry Pi — runs the server 24/7. Any model with networking works; a Pi 4 (2GB+) is comfortable. (Use ethernet for most reliable connection.)
- microSD card (16GB+) with Raspberry Pi OS, plus a reliable power supply.
- A separate machine with a browser (e.g. a Mac), since the Pi is headless use Raspberry Pi Connect or ssh into machine.

### Software / Runtime
- Raspberry Pi OS (Debian-based Linux).
- Python 3 with pip.
- systemd — keeps webhook-server.service alive and drives the hourly webhook-refresh.timer.
- cloudflared — Cloudflare Tunnel daemon exposing the Pi at api.alcoberlabs.xyz without opening ports.

### Accounts & Credentials
- Notion integration — internal integration token plus the database/block IDs the server reads and writes.
- OpenAI API key — for the GPT-4o-mini summarization and callout formatting.
- Google Cloud OAuth client (Desktop app type) — credentials.json, used to generate token.json with the calendar.readonly scope.
- A registered domain — you need to use your own. Register it with any registrar (or Cloudflare Registrar) and point its nameservers at Cloudflare.
- Cloudflare account — free tier is fine; hosts DNS for the domain and runs the named tunnel.

### Credential Files
- credentials.json — Google OAuth client secrets.
- token.json — authorized Google Calendar token

### Packages Necessary
- pip install -r requirements.txt

## Webhook Event Flow
````mermaid
flowchart TB
    L[Scheduler] --> M[refresh.py]
    M --> F

    A[Notion Workspace] --> B[FastAPI: /notion-webhook]
    B --> C[Event Queue]
    C --> D[Background Worker]
    D --> E{Debounce: 20s passed?}
    D --> H{Page created?}
    E -->|Yes| F[Refresh deadline + important-things callouts]
    E -->|No| G[Drop event, do nothing]
    H -->|Yes| I[OpenAI: classify + summarize note]
    H -->|No| K[Not a new page, do nothing]
    I --> J[Write summary back to page]
````

## How it works (Updated 6/22/2026)

1. ### Add a new task to Todo database using the "Quick Thought" button

![Image showing the todo database and quick add button](docs/images/image1.png)

2. ### Notion will send a webhook to server. The server will generate a helpful note about the item through the OpenAI API and insert into the todo description for the todo item.

![Image showing the AI generated description for the todo item](docs/images/image2.png)

3. ### Add your own projects and tasks in each project

![Image showing the projects database](docs/images/image3.png)
![Image showing the projects tasks](docs/images/image4.png)

4. ### The due dates for every item in the Todo database and Task item from projects will appear here

![Image showing the deadlines of every todo and task item](docs/images/image5.png)

5. ### A dynamic callout based off the time of the day and how many tasks are due, events planned for today or tomorrow, as well as recommendations for planning sleep time.

![Image showing the important things today](docs/images/image6.png)

## Future updates

- Budget tracker in the important things today callout
- Planning todos suggestments for tomorrow
- Add testing webhooks
