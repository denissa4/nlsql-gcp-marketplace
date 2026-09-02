{{/*
Name of the Service the NLSQL chart created for the release under test. This must
stay in sync with the `nlsql.fullname` helper in chart/nlsql/templates/_helpers.tpl:
when the release name already contains the chart name, Helm does not prefix it again.
*/}}
{{- define "nlsql-tester.targetService" -}}
{{- if .Values.targetService }}
{{- .Values.targetService }}
{{- else }}
{{- $target := default .Release.Name .Values.targetRelease }}
{{- if contains "nlsql" $target }}
{{- $target | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-nlsql" $target | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}
