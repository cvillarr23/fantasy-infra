{{/*
An image reference. A digest pins it (repository@digest, like the kustomize `images:`
transformer); otherwise repository:tag.
Argo CD Image Updater's digest strategy writes the tag as `latest@sha256:...`. That renders
as repository@digest too, so a pinned image keeps one spelling however it was pinned.
Usage: include "fantasy.image" .Values.images.core
*/}}
{{- define "fantasy.image" -}}
{{- if .digest -}}
{{ .repository }}@{{ .digest }}
{{- else if contains "@" (.tag | toString) -}}
{{ .repository }}@{{ splitList "@" .tag | last }}
{{- else -}}
{{ .repository }}:{{ .tag }}
{{- end -}}
{{- end }}

{{/*
In-cluster DNS name of a Service in the release namespace.
Usage: include "fantasy.svcHost" (list $ "fantasy-db")
*/}}
{{- define "fantasy.svcHost" -}}
{{ index . 1 }}.{{ (index . 0).Release.Namespace }}.svc.cluster.local
{{- end }}

{{- define "fantasy.dbHost" -}}
{{- if .Values.postgres.enabled -}}
{{ include "fantasy.svcHost" (list . "fantasy-db") }}
{{- else -}}
{{ required "postgres.host is required when postgres.enabled is false" .Values.postgres.host }}
{{- end -}}
{{- end }}

{{- define "fantasy.redisHost" -}}
{{- if .Values.redis.enabled -}}
{{ include "fantasy.svcHost" (list . "fantasy-cache") }}
{{- else -}}
{{ required "redis.host is required when redis.enabled is false" .Values.redis.host }}
{{- end -}}
{{- end }}

{{- define "fantasy.coreApiHost" -}}
{{ include "fantasy.svcHost" (list . "fantasy-core-pipeline") }}:8000
{{- end }}

{{/* imagePullSecrets for a pod spec, or nothing when none are set. */}}
{{- define "fantasy.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{- range . }}
  - name: {{ . }}
{{- end }}
{{- end }}
{{- end }}

{{/* The job catalog the launcher and the Jobs API read: the user's, or the bundled one. */}}
{{- define "fantasy.catalog" -}}
{{- .Values.launcher.catalog | default (.Files.Get "files/catalog.yaml") -}}
{{- end }}

{{/*
A copied manifest file with its homelab `namespace: default` moved to the release
namespace. Used for the Grafana alerts and dashboards, whose bodies hold Grafana's own
{{ }} templates and so can't be Helm templates themselves.
*/}}
{{- define "fantasy.rawFiles" -}}
{{- $root := index . 0 -}}
{{- range $path, $_ := $root.Files.Glob (index . 1) }}
---
{{ $root.Files.Get $path | replace "namespace: default\n" (printf "namespace: %s\n" $root.Release.Namespace) }}
{{- end }}
{{- end }}
