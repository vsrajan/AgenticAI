{{/*
Shared names and labels. The release name prefixes everything, so two
releases can coexist in one namespace if that ever becomes useful.
*/}}

{{- define "agnes.mcpName" -}}
{{ .Release.Name }}-mcp
{{- end }}

{{- define "agnes.agentName" -}}
{{ .Release.Name }}-agent
{{- end }}

{{- define "agnes.secretName" -}}
{{ .Release.Name }}-secrets
{{- end }}

{{- define "agnes.labels" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "agnes.mcpImage" -}}
{{- if .Values.image.registry -}}
{{ .Values.image.registry }}/{{ .Values.image.mcpRepository }}:{{ required "set image.tag (a git sha)" .Values.image.tag }}
{{- else -}}
{{ .Values.image.mcpRepository }}:{{ required "set image.tag (a git sha)" .Values.image.tag }}
{{- end -}}
{{- end }}

{{- define "agnes.agentImage" -}}
{{- if .Values.image.registry -}}
{{ .Values.image.registry }}/{{ .Values.image.agentRepository }}:{{ required "set image.tag (a git sha)" .Values.image.tag }}
{{- else -}}
{{ .Values.image.agentRepository }}:{{ required "set image.tag (a git sha)" .Values.image.tag }}
{{- end -}}
{{- end }}

{{/*
Pod anti-affinity: spread a component's replicas across nodes and
zones (preferred, not required, so small clusters still schedule).
Usage: include "agnes.antiAffinity" (dict "component" "mcp")
*/}}
{{- define "agnes.antiAffinity" -}}
podAntiAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchLabels:
            app.kubernetes.io/component: {{ .component }}
        topologyKey: topology.kubernetes.io/zone
    - weight: 50
      podAffinityTerm:
        labelSelector:
          matchLabels:
            app.kubernetes.io/component: {{ .component }}
        topologyKey: kubernetes.io/hostname
{{- end }}
