{{/*
Shared names and labels. The release name prefixes everything, so two
releases can coexist in one namespace if that ever becomes useful.

On this branch there is ONE workload: the agent, which runs the MCP
server as a child process over stdio (docs/single-container-stdio.md).
The mcp-specific helpers are gone with the mcp Deployment and Service.
*/}}

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

{{/*
ONE image carrying both projects -- the agent and the MCP server share
a container because stdio needs a parent-child process relationship.
*/}}
{{- define "agnes.image" -}}
{{- if .Values.image.registry -}}
{{ .Values.image.registry }}/{{ .Values.image.repository }}:{{ required "set image.tag (a git sha)" .Values.image.tag }}
{{- else -}}
{{ .Values.image.repository }}:{{ required "set image.tag (a git sha)" .Values.image.tag }}
{{- end -}}
{{- end }}

{{/*
Pod anti-affinity: spread a component's replicas across nodes and
zones (preferred, not required, so small clusters still schedule).
Usage: include "agnes.antiAffinity" (dict "component" "agent")
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
