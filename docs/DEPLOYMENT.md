# Hosting Civil Estimator on a server

This is the whole deployment: what to buy, what runs where, who does which
step, and what to check when something looks wrong. It assumes Hetzner, because
that is what you asked about, but only the hardware section is specific to them.

---

## 1. What actually runs

Four processes, all on one machine to start with.

| Process | What it does | Port |
|---|---|---|
| nginx | TLS, and the websocket proxy Streamlit needs | 443 |
| `civil-estimator-web` | the Streamlit app people use | 8501, localhost only |
| `civil-estimator-worker` | reads drawings, one at a time | none |
| `ollama` | serves `qwen2.5vl:7b` to the worker | 11434, localhost only |

The web app never calls the model. That separation is the reason the interface
stays responsive: a drawing takes minutes, and the app's job is to show a
percentage and accept a cancel while that happens elsewhere.

They meet in three places, and the first two must be identical in the
environment file or nothing works:

* `CIVIL_ESTIMATOR_JOBS_DB` — the SQLite queue.
* `CIVIL_ESTIMATOR_CACHE_DIR` — extraction results, keyed by the drawing's
  content hash.
* `CIVIL_ESTIMATOR_SECRETS` — the password hash, kept outside the checkout so a
  deploy cannot overwrite it and an image cannot carry it.

Three more move the app's own files onto the data volume. The worker does not
read them, so they need only be set for the app:

* `CIVIL_ESTIMATOR_UPLOAD_DIR` — where uploaded drawings are written.
* `CIVIL_ESTIMATOR_DRAWING_DIRS` — the other folders searched for drawings,
  separated by `:` (default `Drawings:.`).
* `CIVIL_ESTIMATOR_CLASSIFICATIONS` — the Green / Brown / Repair filing.

A worker that cannot see the app's queue looks exactly like a worker that is
merely slow. If the queue fills and nothing moves, check these values first.

### Running it on a laptop

One command, and it owns both processes:

```bash
./bin/ce            # start both, follow the logs, Ctrl-C stops both
./bin/ce stop       # stop both
./bin/ce status     # what is running, and how the queue looks
```

On the server systemd plays that role, and `bin/ce` is not used.

---

## 2. What to buy

### Hetzner does still sell a GPU server

Correction to what I told you earlier: the GEX44 I named is gone, but the line
is not. Hetzner replaced it in 2026 with two models, and the smaller one is a
good fit for this workload.

| | GEX45 | GEX131 |
|---|---|---|
| GPU | RTX PRO 4000 Blackwell SFF, **24 GB** GDDR7 | RTX PRO 6000 Blackwell Max-Q, 96 GB |
| CPU | Intel Core i5-13500 | Xeon Gold 5412U, 24 cores |
| RAM | 64 GB DDR4 | 256 GB DDR5 ECC |
| Disk | 2 x 512 GB NVMe | — |
| Location | Helsinki | Helsinki |
| Price | **EUR 214/month + EUR 209 setup** | considerably more |

The GEX45 is the recommendation. Twenty-four gigabytes of video memory is the
number that matters, because it is the threshold at which a far more accurate
model becomes available to you. See the next section.

Two caveats. It is a dedicated server, so provisioning is not instant and stock
comes and goes. And Helsinki is further from Riyadh than Falkenstein was:
expect roughly 150 to 190 ms, which makes a Streamlit tab feel perceptibly
behind the cursor without making it unusable.

### If the drawings may not leave the region

This is the question that actually decides the answer, and it is not a
technical one. The model runs on the same machine as the app, so Maaden's
drawings live wherever you put that machine. If they must stay in the Gulf,
Hetzner is out at any price, and you are choosing between the three
hyperscalers that have a Middle East region.

| Option | Region | GPU | Roughly |
|---|---|---|---|
| Hetzner GEX45 | Helsinki | RTX PRO 4000, 24 GB | EUR 214/mo |
| Azure NV18ads_A10_v5 | UAE North | half an A10, 12 GB | ~USD 950/mo |
| Azure NV36ads_A10_v5 | UAE North | full A10, 24 GB | ~USD 1,900/mo |
| AWS g6.xlarge | UAE, Bahrain | L4, 24 GB | ~USD 590/mo plus regional uplift |
| Oracle VM.GPU.A10.1 | Jeddah | A10, 24 GB | ~USD 1,460/mo at list |

Take those as the shape of the gap, not as a quotation. Every one of them
changes, hyperscaler list prices are higher in Middle East regions than in the
US regions most published figures come from, and all three will discount a
committed year.

Two traps worth naming. Azure's NV A10 v5 sizes are **fractions** of a card,
and the cheapest one, NV6ads, gives you 4 GB — not enough to load the model at
all. And an always-on hyperscaler GPU is the expensive way to buy this: your
actual duty cycle is a few hours a week, so if you go that route, price the
reserved instance and consider stopping the VM between sessions.

**My recommendation.** If the drawings may sit in Finland, take the GEX45. It
is a third of the price of the cheapest Middle East option that can actually
hold the model, and the latency costs you a slightly laggy interface, not
accuracy or throughput. If they may not, AWS `g6.xlarge` in UAE or Bahrain is
the cheapest credible choice, and I would run it stopped between sessions.

### If no GPU is approved at all

A Hetzner CCX line cloud server runs everything except the model at sensible
speed. Vision extraction on CPU is slow enough to change how you work: several
minutes per sheet rather than one or two. That is survivable here only because
a drawing is read once and cached forever after, and because the sheets that
kept their text layer never touch the model at all. Size it at 8 dedicated
vCPU and 32 GB.

---

## 2a. Which model to run

You asked whether a different Ollama model would help. It would, and the choice
follows directly from how much video memory you buy.

| VRAM | Model | Size | Why |
|---|---|---|---|
| 8 GB | `qwen3-vl:8b` | 6.1 GB | Drop-in successor to what you run now. Same footprint, better reading. |
| 24 GB | `qwen3-vl:32b` | 21 GB | The accuracy jump. Published OCR comparisons put the 32B class several points above the 7B class on English text, and much further ahead on dense or degraded documents — which is exactly what a scanned A0 callout is. |
| 24 GB | `qwen3-vl:30b` | 20 GB | A mixture-of-experts build. Close to 32B quality at noticeably higher speed; worth benchmarking against 32B on your own sheets. |

Change it in one place, `CIVIL_ESTIMATOR_MODEL` in the environment file, then
restart the worker. Nothing in the extraction code names a model.

Two things to hold on to when you do:

* **Re-read one drawing you already know.** The correction machinery in
  `extractors/corrections.py` scores a model's output against your team's
  marked-up workbook. That is how to decide whether 32B is worth its extra
  seconds on *your* drawings, rather than on a benchmark of somebody else's.
* **The cache is keyed on the drawing, not the model.** Switching models does
  not invalidate it, so re-read deliberately with the force option rather than
  expecting new numbers to appear on their own.

Keep one model warm at a time. Two resident vision models on a 24 GB card means
one of them cold-starts on every call.

---

## 3. Who does what

**What I can do from here**, and have:

* every file under `deploy/` — systemd units, the nginx site, the environment
  template, and `install.sh`;
* the app and worker split, so the interface stays responsive;
* environment-overridable data paths, so nothing is pinned to a developer's
  home directory;
* the health signals the guide tells you to check.

**What has to be you.** Not a limitation of effort — these need your account,
your domain and your password, and I should not hold any of them:

1. Order the server and receive the root credentials.
2. Point a DNS record at its IP address.
3. Run the installer and obtain the TLS certificate.
4. Set the application password.
5. Decide who is allowed in.

---

## 4. Install

Copy the repository up, excluding the things that are rebuilt on the server:

```bash
rsync -a --exclude venv --exclude output --exclude .git \
  ./ root@YOUR_SERVER_IP:/opt/civil-estimator/
```

Then, on the server:

```bash
cd /opt/civil-estimator && bash deploy/install.sh estimator.yourdomain.com
```

It installs the packages, creates the `estimator` service user, builds the
virtual environment, installs and starts Ollama, pulls the model, installs both
systemd units and writes the nginx site. It is safe to run twice.

The certificate and DNS stay yours:

```bash
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d estimator.yourdomain.com
```

Set the password last, because the app refuses every login until one exists:

```bash
sudo -u estimator /opt/civil-estimator/venv/bin/python bin/set_password.py
```

---

## 5. Checking it works

```bash
systemctl status civil-estimator-web civil-estimator-worker ollama
journalctl -u civil-estimator-worker -f
```

A healthy worker log is quiet until something is queued, then prints one line
per drawing with the outcome and the time it took. `cache` in that line means
the drawing had been read before and cost nothing.

Three failures and what they look like:

| What you see | What it means |
|---|---|
| Queue fills, nothing moves, app says to start the worker | the worker is down, or its `CIVIL_ESTIMATOR_JOBS_DB` differs from the app's |
| Jobs fail with "the vision model is unavailable" | `ollama` is not running, or the model was never pulled |
| App loads, then never updates | nginx is missing the `Upgrade` headers |

---

## 6. Backups

Two directories hold everything that cannot be regenerated:

* `/var/lib/civil-estimator` — the queue and the extraction cache.
* `/opt/civil-estimator/output` — uploaded drawings and generated workbooks.

A Hetzner Storage Box over SSH is the cheap option. Back up the SQLite queue
with `sqlite3 jobs.db ".backup ..."` rather than copying the file, because a
plain copy of a database in WAL mode can be taken mid-write:

```bash
sqlite3 /var/lib/civil-estimator/jobs.db ".backup /tmp/jobs-backup.db"
rsync -a /tmp/jobs-backup.db /opt/civil-estimator/output/ uXXXXX@uXXXXX.your-storagebox.de:backup/
```

Losing the cache costs model time, not data. Losing `output/` loses drawings.

---

## 7. Database: stay on SQLite

For this workload SQLite is the right answer, and I would not change it yet.

The reasoning is about where the bottleneck is. The model serves one request at
a time, so the queue is drained serially no matter how many people are using
the app. There is no write concurrency for PostgreSQL to win. Meanwhile the
real data is files, and a database would not be holding them.

SQLite is configured here in WAL mode with a busy timeout, so the app reads the
queue while the worker writes it, and job claims are taken inside an immediate
transaction so two workers can never take the same drawing.

**Move to PostgreSQL when one of these becomes true:**

* you run more than one worker, on more than one machine;
* the data directory moves to a network filesystem, where SQLite's locking is
  genuinely unsafe;
* you need per-user accounts with roles, audit trails and a managed backup
  schedule that the rest of your estate already has.

The first is the one to watch. A second GPU server is the natural way to make a
twenty-drawing set faster, and that is the point at which the queue needs a real
database behind it.

One correction worth stating plainly: a database change will not improve
extraction accuracy. Accuracy comes from the extractor and from the correction
loop in `extractors/corrections.py`, where your team's marked-up workbooks are
scored against what the model read.

---

## 8. Before you let a team in

The app has **one shared password**. Everyone who logs in is the same person as
far as the queue is concerned, which means anyone can cancel anyone's batch.
That is tolerable for a handful of colleagues who can talk to each other, and
not tolerable beyond that.

The queue already carries an `owner` column on every job and filters by it, so
per-user accounts can be added without touching the schema or migrating data.
Until then:

* put the server behind your VPN, or restrict port 443 with the Hetzner
  firewall to the offices that need it;
* keep the password out of email;
* remember that uploaded drawings are client material sitting on a rented
  machine, and confirm that is acceptable to Maaden before the first upload.

---

## 9. Updating

```bash
rsync -a --exclude venv --exclude output --exclude .git ./ root@SERVER:/opt/civil-estimator/
ssh root@SERVER '
  sudo -u estimator /opt/civil-estimator/venv/bin/pip install -q -r /opt/civil-estimator/requirements.txt
  systemctl restart civil-estimator-web civil-estimator-worker'
```

Restarting the worker mid-drawing is safe. The job goes back to the queue and is
read again from the start; nothing half-written is kept.

---

## 10. The Docker plan

Not built yet, by your instruction. This is the whole design so that "go ahead"
is a short job rather than a new conversation.

### Why containers help here, and where they do not

They help because this is four processes that must agree on two paths and one
model name, and because the thing most likely to break a rebuild is the Python
environment rather than our code. They do **not** help with the GPU: the model
still needs the host's NVIDIA driver, and that is a host install either way.

### What gets built

Three images, one of which is not ours.

| Service | Image | Notes |
|---|---|---|
| `web` | ours, from `Dockerfile` | Streamlit, no model libraries needed |
| `worker` | **the same image**, different command | identical dependencies, so one build |
| `ollama` | `ollama/ollama` official | GPU passed through, model on a named volume |

One image for web and worker is deliberate. They import the same code and must
never drift apart in library versions; two Dockerfiles is two chances to find
that out in production.

### The pieces, in order

1. **`Dockerfile`** — `python:3.12-slim`, install `requirements.txt`, copy the
   app, create a non-root user, no `CMD` baked in. The compose file supplies the
   command, which is what lets one image be two services.
2. **`.dockerignore`** — `venv/`, `output/`, `.git/`, `data/`, `__pycache__`,
   `Drawings/`. Without it the build context carries every A0 you have ever
   uploaded and the build crawls.
3. **`compose.yaml`** — the four services, one network, three named volumes
   (`ce-data` for the queue and cache, `ce-output` for drawings and workbooks,
   `ollama-models`). The GPU reservation goes on the `ollama` service only.
4. **`compose.cpu.yaml`** — an override that drops the GPU reservation, for a
   server without one and for your own laptop.
5. **A healthcheck on each** — Streamlit answers on `/_stcore/health`; the
   worker's is a queue read, which proves it can reach the database the web
   service writes to. That is the failure this whole file exists to catch early.

### The compose file, in outline

```yaml
services:
  ollama:
    image: ollama/ollama
    volumes: [ollama-models:/root/.ollama]
    deploy:                      # drop this block in compose.cpu.yaml
      resources:
        reservations:
          devices: [{driver: nvidia, count: 1, capabilities: [gpu]}]

  web:
    build: .
    command: streamlit run Home.py --server.port 8501 --server.address 0.0.0.0
    env_file: [deploy/civil-estimator.env]
    volumes: [ce-data:/var/lib/civil-estimator, ce-output:/app/output]
    depends_on: [ollama]
    ports: ["127.0.0.1:8501:8501"]

  worker:
    build: .
    command: python bin/worker.py
    env_file: [deploy/civil-estimator.env]
    volumes: [ce-data:/var/lib/civil-estimator, ce-output:/app/output]
    depends_on: [ollama]
    stop_grace_period: 15m       # let it finish the drawing it is on

  nginx:
    image: nginx
    ports: ["80:80", "443:443"]
    volumes: [./deploy/nginx-civil-estimator.conf:/etc/nginx/conf.d/default.conf:ro]
```

Four details in there are load-bearing rather than decoration:

* **`stop_grace_period: 15m`.** Compose's default is ten seconds. A `docker
  compose restart` would kill the worker mid-drawing on every deploy, throwing
  away minutes of model time each time.
* **Both app and worker mount `ce-data`.** They must see the same queue file.
  This is the single most likely thing to get wrong.
* **`ports: 127.0.0.1:8501`.** Without the host prefix, Docker punches through
  the firewall and publishes Streamlit on the public interface with no
  certificate.
* **`OLLAMA_HOST=http://ollama:11434`** in the env file, not `127.0.0.1`.
  Inside a container, localhost is the container.

### What changes in this repo

Very little, which is the point of having done the environment variables first:

* `CIVIL_ESTIMATOR_JOBS_DB` and `CIVIL_ESTIMATOR_CACHE_DIR` already redirect the
  two stateful paths, and compose points both at the shared volume.
* `CIVIL_ESTIMATOR_UPLOAD_DIR`, `CIVIL_ESTIMATOR_DRAWING_DIRS` and
  `CIVIL_ESTIMATOR_CLASSIFICATIONS` do the same for uploads, the drawing set and
  the site classifications.
* `OLLAMA_HOST` already comes from the environment.
* `bin/ce` stays the way you run it on a laptop; compose replaces it on the
  server. Neither knows about the other.

### Preparation before the day

1. Decide the residency question in section 2. It determines the provider, and
   everything else follows.
2. Order the server and get a domain pointed at it.
3. Decide the model from section 2a, so the first pull is the right one.
4. Get agreement that client drawings may sit on that machine.
5. Tell me which, and I will build the five files above, run the stack locally
   against the real drawings, and hand you a single `docker compose up -d`.

### Deployment day, in order

```bash
git clone <repo> /opt/civil-estimator && cd /opt/civil-estimator
cp deploy/civil-estimator.env.example deploy/civil-estimator.env   # edit it
docker compose up -d
docker compose exec ollama ollama pull qwen3-vl:8b
docker compose exec web python bin/set_password.py
certbot --nginx -d estimator.yourdomain.com
```

Then the check that matters more than any of the above: queue one drawing you
already know the answer to, and compare the workbook against the marked-up copy
your team produced on the laptop. A deployment that starts is not a deployment
that is right.
