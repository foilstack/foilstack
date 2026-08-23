# The encoder

Every reference card and every scan is turned into a vector by a DINOv3 model
running in its own container. It is the slowest part of the system and the only
part that wants a GPU.

## CPU by default

The stack starts anywhere, including a laptop with no CUDA toolkit, because the
image installs CPU torch wheels. That is the right default and it is also
genuinely slow: roughly **one card per second**, most of it the model rather
than the network.

At that rate a full Magic catalogue — around a hundred thousand printings — is
better than a day of wall clock.

## Turning the GPU on

An overlay rather than a flag, because a device reservation on a machine
without a GPU fails at `up` rather than degrading:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Set it once and every plain `docker compose` command picks it up:

```bash
# .env
COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
```

Pick the wheel index that matches your driver — `nvidia-smi` prints the CUDA
version it supports in the top right:

| index | driver | notes |
|---|---|---|
| `cpu` | — | the default |
| `cu126` | 525+ | |
| `cu128` | 570+ | Blackwell, sm_120 |
| `cu130` | 580+ | CUDA 13; GB10 / DGX Spark is sm_121 |

Override with `FOILSTACK_TORCH_INDEX` if the default in the overlay is wrong for
your machine.

It needs the [NVIDIA Container
Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host. Check it independently of foilstack:

```bash
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
```

## Checking which one you got

The encoder chooses for itself and says so on the first line of its log. There
is no failure mode where a missing GPU stops it working — only one where it is
slow, which is easy to miss:

```bash
docker compose logs embedder | grep 'encoder loaded'
# encoder loaded: facebook/dinov3-vitl16-pretrain-lvd1689m on cuda (torch.bfloat16)
```

`on cpu (torch.float32)` means it did not find one. Either the overlay is not
in play, the toolkit is not installed, or the wheels are a CPU build — the last
one is the quiet case, because everything else looks correct.

## Filling the catalogue

`ingest` is cheap and `embed` is not. Ingest talks to the catalogue API and
finishes in seconds per set; embedding downloads and encodes one image per
printing:

```bash
docker compose exec web foilstack ingest --source tcgcsv --game pokemon
docker compose exec web foilstack embed --concurrency 8
```

`--concurrency` overlaps the download of the next card with the encoding of the
current one, which is where most of the wall clock goes on CPU. The images come
from a CDN rather than from the catalogue API, so the pacing that governs
`sync-prices` does not apply — but the client still identifies itself, backs off
on 429 and honours `Retry-After`. Eight is a polite default; there is no prize
for making it forty.

Both commands are resumable. `embed` skips anything already encoded with the
current model, so an interrupted run costs the interruption rather than the run.

**A catalogue you have not ingested is a card the matcher cannot return.**
Nearest-neighbour search answers with the closest thing it holds, so scanning
Magic against a Pokémon-only catalogue returns Pokémon — confidently, and with a
score low enough to notice only if you are looking.
