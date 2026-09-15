{{/*
Expand the name of the chart.
*/}}
{{- define "nlsql.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name. Cloud Marketplace requires every resource name to be
prefixed with the app instance name (the Helm release name), so that two instances
can coexist in one namespace.
*/}}
{{- define "nlsql.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "nlsql.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "nlsql.labels" -}}
helm.sh/chart: {{ include "nlsql.chart" . }}
{{ include "nlsql.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "nlsql.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nlsql.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "nlsql.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "nlsql.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret holding ApiToken / AppPassword / DbPassword. Either a Secret the
customer created beforehand, or the one this chart renders.
*/}}
{{- define "nlsql.secretName" -}}
{{- if .Values.credentials.existingSecret }}
{{- .Values.credentials.existingSecret }}
{{- else }}
{{- printf "%s-secrets" (include "nlsql.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Fully qualified image reference. Prefers an immutable digest over a mutable tag;
Cloud Marketplace requires documented CLI installs to pin by digest.
*/}}
{{- define "nlsql.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repo .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repo (.Values.image.tag | toString) }}
{{- end }}
{{- end }}

{{/*
Fully qualified image reference for the Cloud Marketplace metering agent, on the
same digest-over-tag rule as the app image.
*/}}
{{- define "nlsql.ubbagentImage" -}}
{{- if .Values.ubbagent.image.digest }}
{{- printf "%s@%s" .Values.ubbagent.image.repo .Values.ubbagent.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.ubbagent.image.repo (.Values.ubbagent.image.tag | toString) }}
{{- end }}
{{- end }}
