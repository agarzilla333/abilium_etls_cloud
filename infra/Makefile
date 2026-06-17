# Thin wrappers around the deploy workflow. Run from infra/.
#   make build      # Cloud Build -> Artifact Registry (tag = git short SHA)
#   make plan       # terraform plan for that image
#   make deploy     # terraform apply (creates/updates the stack)
#   make destroy

REGION  ?= us-central1
PROJECT ?= $(shell terraform output -raw project 2>/dev/null)
TAG     ?= $(shell git rev-parse --short HEAD)
ROOT    := $(abspath ..)
IMAGE    = $(REGION)-docker.pkg.dev/$(PROJECT)/abilium/app:$(TAG)

.PHONY: build plan deploy destroy init repo

# First-time only: create the Artifact Registry repo + enable APIs so `build` has
# somewhere to push (before the full apply, which needs the image to exist).
repo:
	terraform apply -target=google_project_service.apis -target=google_artifact_registry_repository.app

build:
	gcloud builds submit $(ROOT) --tag $(IMAGE) --project $(PROJECT)

plan:
	terraform plan -var image_tag=$(TAG)

deploy:
	terraform apply -var image_tag=$(TAG)

destroy:
	terraform destroy

init:
	terraform init
