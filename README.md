<img src="https://user-images.githubusercontent.com/12534576/192582340-4c9e4401-1fe6-4dbb-95bb-fdbba5493f61.png"/>

![GitHub](https://img.shields.io/github/license/heartexlabs/label-studio?logo=heartex) ![label-studio:build](https://github.com/HumanSignal/label-studio/workflows/label-studio:build/badge.svg) ![GitHub release](https://img.shields.io/github/v/release/heartexlabs/label-studio?include_prereleases)

[Website](https://labelstud.io/) • [Docs](https://labelstud.io/guide/) • [Join Slack Community <img src="https://app.heartex.ai/docs/images/slack-mini.png" width="18px"/>](https://slack.labelstud.io/?source=github-1)


## What is Label Studio?

<!-- <a href="https://labelstud.io/blog/release-130.html"><img src="https://github.com/HumanSignal/label-studio/raw/master/docs/themes/htx/source/images/release-130/LS-Hits-v1.3.png" align="right" /></a> -->

Label Studio is an open source data labeling tool. It lets you label data types like audio, text, images, videos, and time series with a simple and straightforward UI and export to various model formats. It can be used to prepare raw data or improve existing training data to get more accurate ML models.

- [Try out Label Studio](#try-out-label-studio)
- [What you get from Label Studio](#what-you-get-from-label-studio)
- [Included templates for labeling data in Label Studio](#included-templates-for-labeling-data-in-label-studio)
- [Set up machine learning models with Label Studio](#set-up-machine-learning-models-with-Label-Studio)
- [Integrate Label Studio with your existing tools](#integrate-label-studio-with-your-existing-tools)

![Gif of Label Studio annotating different types of data](/images/annotation_examples.gif)

Have a custom dataset? You can customize Label Studio to fit your needs. Read an [introductory blog post](https://towardsdatascience.com/introducing-label-studio-a-swiss-army-knife-of-data-labeling-140c1be92881) to learn more. 

## Try out Label Studio

Install Label Studio locally or deploy it in a cloud instance. [Or sign up for a free trial of our Starter Cloud edition!](https://humansignal.com/platform/starter-cloud/) You can learn more about what each edition offers [here](https://labelstud.io/guide/label_studio_compare). 

- [Install locally with Docker](#install-locally-with-docker)
- [Run with Docker Compose (Label Studio + Nginx + PostgreSQL)](#run-with-docker-compose)
- [Install locally with pip](#install-locally-with-pip)
- [Install locally with poetry](#install-locally-with-poetry)
- [Install locally with Anaconda](#install-locally-with-anaconda)
- [Install for local development](#install-for-local-development)
- [Deploy in a cloud instance](#deploy-in-a-cloud-instance)

### Install locally with Docker
Official Label Studio docker image is [here](https://hub.docker.com/r/heartexlabs/label-studio) and it can be downloaded with `docker pull`. 
Run Label Studio in a Docker container and access it at `http://localhost:8080`.


```bash
docker pull heartexlabs/label-studio:latest
docker run -it -p 8080:8080 -v $(pwd)/mydata:/label-studio/data heartexlabs/label-studio:latest
```
You can find all the generated assets, including SQLite3 database storage `label_studio.sqlite3` and uploaded files, in the `./mydata` directory.

#### Override default Docker install
You can override the default launch command by appending the new arguments:
```bash
docker run -it -p 8080:8080 -v $(pwd)/mydata:/label-studio/data heartexlabs/label-studio:latest label-studio --log-level DEBUG
```

#### Build a local image with Docker
If you want to build a local image, run:
```bash
docker build -t heartexlabs/label-studio:latest .
```

### Run with Docker Compose
Docker Compose script provides production-ready stack consisting of the following components:

- Label Studio
- [Nginx](https://www.nginx.com/) - proxy web server used to load various static data, including uploaded audio, images, etc.
- [PostgreSQL](https://www.postgresql.org/) - production-ready database that replaces less performant SQLite3.

To start using the app from `http://localhost` run this command:
```bash
docker-compose up
```

### Run with Docker Compose + MinIO
You can also run it with an additional MinIO server for local S3 storage. This is particularly useful when you want to 
test the behavior with S3 storage on your local system. To start Label Studio in this way, you need to run the following command:
````bash
# Add sudo on Linux if you are not a member of the docker group
docker compose -f docker-compose.yml -f docker-compose.minio.yml up -d
````
If you do not have a static IP address, you must create an entry in your hosts file so that both Label Studio and your 
browser can access the MinIO server. For more detailed instructions, please refer to [our guide on storing data](docs/source/guide/storedata.md).


### Install locally with pip

```bash
# Requires Python >=3.10
pip install label-studio

# Start the server at http://localhost:8080
label-studio
```

### Install locally with poetry

```bash
### install poetry
pip install poetry

### set poetry environment
poetry new my-label-studio
cd my-label-studio
poetry add label-studio

### activate poetry environment
poetry shell

### Start the server at http://localhost:8080
label-studio
```

### Install for local development

You can run the latest Label Studio version locally without installing the package from pypi. 

```bash
# Install all package dependencies
pip install poetry
poetry install

# Run database migrations
poetry run python3 label_studio/manage.py migrate
poetry run python3 label_studio/manage.py collectstatic

# Start the server in development mode at http://localhost:8080
poetry run python3 label_studio/manage.py runserver 0.0.0.0:8080
```

To build the frontend:

```bash
cd web
yarn install
yarn build
```

### Deploy in a cloud instance

You can deploy Label Studio with one click in Heroku, Microsoft Azure, or Google Cloud Platform: 

<a href="https://www.heroku.com/deploy?template=https://github.com/HumanSignal/label-studio/tree/heroku-persistent-pg"><img src="https://www.herokucdn.com/deploy/button.svg" alt="Deploy" height="30px"></a>
[<img src="https://aka.ms/deploytoazurebutton" height="30px">](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Fhumansignal%2Flabel-studio%2Fdevelop%2Fazuredeploy.json)
[<img src="https://deploy.cloud.run/button.svg" height="30px">](https://deploy.cloud.run)


#### Apply frontend changes

For information about updating the frontend, see [label-studio/web/README.md](https://github.com/HumanSignal/label-studio/blob/develop/web/README.md#installation-instructions).


#### Install dependencies on Windows 
To run Label Studio on Windows, download and install the following wheel packages from [Gohlke builds](https://www.lfd.uci.edu/~gohlke/pythonlibs) to ensure you're using the correct version of Python:
- [lxml](https://www.lfd.uci.edu/~gohlke/pythonlibs/#lxml)

```bash
# Upgrade pip 
pip install -U pip

# If you're running Win64 with Python 3.8, install the packages downloaded from Gohlke:
pip install lxml‑4.5.0‑cp38‑cp38‑win_amd64.whl

# Install label studio
pip install label-studio
```

### Run test suite
To add the tests' dependencies to your local install:

```bash
poetry install --with test
```

Alternatively, it is possible to run the unit tests from a Docker container in which the test dependencies are installed:


```bash
make build-testing-image
make docker-testing-shell
```

In either case, to run the unit tests:

```bash
cd label_studio

# sqlite3
DJANGO_DB=sqlite DJANGO_SETTINGS_MODULE=core.settings.label_studio pytest -vv

# postgres (assumes default postgres user,db,pass. Will not work in Docker
# testing container without additional configuration)
DJANGO_DB=default DJANGO_SETTINGS_MODULE=core.settings.label_studio pytest -vv
```
 
## Role-Based Access Control (RBAC)

This fork adds a built-in RBAC system with three roles:

| Role | Description |
|-|-|
| **Admin** | Full access. Can create projects, import/export data, manage organization members, assign roles, and configure all project settings. |
| **QA (Supervisor)** | Reviews annotations submitted by labellers. Can accept or reject annotations with comments. Can assign members to projects. Cannot create projects, import/export data, label tasks, or access the organization page. QA must be assigned to projects to see them. |
| **Labeller** | Can only view and label tasks in projects they are assigned to. Cannot create projects, import/export data, access organization settings, or modify project settings. |

### Key features

- **Role assignment**: Admins can assign roles (Admin, QA, Labeller) to organization members from the Organization page.
- **Project-level access control**: Admins and QA can assign members to specific projects via the **Members** tab in project settings. Both QA and Labellers must be assigned to a project to see it.
- **QA review workflow**: QA users can accept or reject each annotation with an optional comment (required for rejection). The review status is stored per annotation.
- **Review status visibility**: When viewing a task, Admin and Labeller users see a review status panel showing whether each annotation has been accepted, rejected, or is pending review, along with any QA comments.
- **UI restrictions**: The interface adapts based on the user's role -- non-admin users see a simplified UI without create/import/export controls. QA users see a read-only labeling view with a review panel.
- **Role indicator**: The current user's role is displayed next to the avatar in the top-right corner of the header.

### Role permissions summary

| Action | Admin | QA | Labeller |
|-|-|-|-|
| Create/delete projects | Yes | No | No |
| Import/export data | Yes | No | No |
| Full project settings | Yes | No | No |
| Assign members to projects | Yes | Yes | No |
| View organization page | Yes | No | No |
| Manage member roles | Yes | No | No |
| View projects | All | Assigned only | Assigned only |
| Label tasks | Yes | No | Yes |
| Update own annotations | Yes | No | Yes |
| Review annotations (accept/reject) | Yes | Yes | No |
| View review status | Yes | Read-only | Read-only |

### QA review workflow

1. Admin creates a project, imports data, and assigns QA and Labellers via **Settings > Members**
2. Labellers open assigned projects, label tasks, and submit annotations
3. QA opens assigned projects and clicks on tasks to review annotations
4. QA sees a read-only view of the labeling interface with a **Review panel** in the bottom-right
5. QA clicks **Accept** or **Reject** (with a required comment for rejection)
6. Labellers and Admins see the review status badge when viewing a task, including any QA comments

### Updating reviewed annotations

When a Labeller or Admin updates an annotation that has already been reviewed:

- **Accepted annotations**: A confirmation dialog asks whether to proceed. If confirmed, the review status resets to **Pending** and the annotation goes back through the review workflow.
- **Rejected annotations**: The update proceeds immediately (no confirmation needed) and the review status resets to **Pending** automatically.

This ensures that any changes made after a review always get re-reviewed by QA.

### QA draft behavior

QA users do not create or save drafts. Since QA reviews annotations rather than authoring them, all draft-related functionality (autosave, navigation draft saves, and draft guard prompts) is disabled for QA users.

## Microphone Recording Plugin

This fork adds a `<Microphone>` tag for audio recording directly in the labeling interface. Annotators can record audio from their microphone, play it back with a waveform visualizer, and re-record before submitting.

### Example labeling configs

**Standalone recording (no control tags needed):**

```xml
<View>
  <Text name="text" value="$text" />
  <Microphone name="mic" />
</View>
```

**Recording with transcription:**

```xml
<View>
  <Text name="prompt" value="$prompt" />
  <Microphone name="mic" />
  <TextArea name="transcription" toName="mic"
            placeholder="Transcribe what you recorded" />
</View>
```

### Attributes

| Attribute | Default | Description |
|-|-|-|
| `name` | (required) | Tag identifier |
| `maxduration` | `300` | Maximum recording length in seconds |
| `format` | `webm` | Audio format (`webm` or `wav`) |

### Features

- **Live waveform** during recording with scrolling visualization
- **Waveform playback** after recording with seek, play/pause, and time display
- **Auto-upload** recordings are automatically uploaded to the server after recording stops
- **Playback on revisit** previously recorded audio is loaded and playable when revisiting a submitted task
- **Re-record** re-record and update submissions
- **Audio column in data manager** recorded audio appears as a playable column in the project data view
- **Keyboard shortcuts** for fast annotation workflow
- **Secure context required** microphone access requires HTTPS or `http://localhost`

### Keyboard shortcuts

| Key | Action |
|-|-|
| `R` | Start recording (idle) / Re-record (playback) |
| `S` | Stop recording |
| `Space` | Toggle: Record (idle) / Stop (recording) / Play-Pause (playback) |

### Example task data

```json
{ "text": "Please read this sentence aloud" }
```

### Storage and export

- Recorded audio is always saved to Label Studio's built-in upload directory for browser playback
- If a **local export storage** is configured (Project Settings > Cloud Storage > Target Storage), audio files are also copied there automatically
- The exported annotation result includes the storage path:

```json
{
  "from_name": "mic",
  "to_name": "mic",
  "type": "microphone",
  "value": {
    "audio_url": "/path/to/target-storage/abc-mic-mic-1712745600000.webm",
    "playback_url": "/data/upload/5/abc-mic-mic-1712745600000.webm"
  }
}
```

| Field | Description |
|-|-|
| `audio_url` | Path in target storage (for your data pipeline). Falls back to the playback URL if no target storage is configured. |
| `playback_url` | Serveable URL for browser playback. Only present when target storage is configured. |

### Notes

- The `<Microphone>` tag works as a standalone object tag -- no control tag (like `Choices` or `TextArea`) is required, though you can add them if needed
- When exporting to CSV, the `mic` column contains the audio URL as JSON
- The data manager shows a **Recording** column with the audio for each task

## What you get from Label Studio

https://github.com/user-attachments/assets/525ad5ff-6904-4398-b507-7e8954268d69

- **Multi-user labeling** sign up and login, when you create an annotation it's tied to your account.
- **Multiple projects** to work on all your datasets in one instance.
- **Streamlined design** helps you focus on your task, not how to use the software.
- **Configurable label formats** let you customize the visual interface to meet your specific labeling needs.
- **Support for multiple data types** including images, audio, text, HTML, time-series, and video. 
- **Import from files or from cloud storage** in Amazon AWS S3, Google Cloud Storage, or JSON, CSV, TSV, RAR, and ZIP archives. 
- **Integration with machine learning models** so that you can visualize and compare predictions from different models and perform pre-labeling.
- **Embed it in your data pipeline** REST API makes it easy to make it a part of your pipeline

## Included templates for labeling data in Label Studio 

Label Studio includes a variety of templates to help you label your data, or you can create your own using specifically designed configuration language. The most common templates and use cases for labeling include the following cases:

<img src="/images/template-types.png" />

## Set up machine learning models with Label Studio

Connect your favorite machine learning model using the Label Studio Machine Learning SDK. Follow these steps:

1. Start your own machine learning backend server. See [more detailed instructions](https://github.com/HumanSignal/label-studio-ml-backend).
2. Connect Label Studio to the server on the model page found in project settings.

This lets you:

- **Pre-label** your data using model predictions. 
- Do **online learning** and retrain your model while new annotations are being created. 
- Do **active learning** by labeling only the most complex examples in your data.

## Integrate Label Studio with your existing tools

You can use Label Studio as an independent part of your machine learning workflow or integrate the frontend or backend into your existing tools.  

## Ecosystem

| Project | Description |
|-|-|
| label-studio | Server, distributed as a pip package |
| [Frontend library](web/libs/editor/) | The Label Studio frontend library. This uses React to build the UI and mobx-state-tree for state management. |  
| [Data Manager library](web/libs/datamanager/) | A library for the Data Manager, our data exploration tool. | 
| [label-studio-converter](https://github.com/HumanSignal/label-studio-sdk/tree/master/src/label_studio_sdk/converter) | Encode labels in the format of your favorite machine learning library |
| [label-studio-transformers](https://github.com/HumanSignal/label-studio-transformers) | Transformers library connected and configured for use with Label Studio |

## Citation

Include a citation for Label Studio in the **References** section of your articles:

```tex
@misc{Label Studio,
  title={{Label Studio}: Data labeling software},
  url={https://github.com/HumanSignal/label-studio},
  note={Open source software available from https://github.com/HumanSignal/label-studio},
  author={
    Maxim Tkachenko and
    Mikhail Malyuk and
    Andrey Holmanyuk and
    Nikolai Liubimov},
  year={2020-2025},
}
```

## License

This software is licensed under the [Apache 2.0 LICENSE](/LICENSE) © [Heartex](https://www.heartex.com/). 2020-2025

<img src="https://user-images.githubusercontent.com/12534576/192582529-cf628f58-abc5-479b-a0d4-8a3542a4b35e.png" title="Hey everyone!" width="180" />
