# Scope of Works

Reference: {{ scope_id }}{% if version %} (version {{ version }}){% endif %}

{% for area in areas %}
## {{ area.name }}

{% for trade, items in area['items'] | groupby('category') %}
### {{ trade }}

| Item | Quantity | Unit |
| --- | --- | --- |
{% for item in items %}| {{ item.description }} | {{ item.quantity }} | {{ item.unit }} |
{% endfor %}
{% endfor %}
{% endfor %}
## Exclusions

{% for e in exclusions %}- {{ e }}
{% else %}- None stated.
{% endfor %}

## Assumptions

{% for a in assumptions %}- {{ a }}
{% else %}- None stated.
{% endfor %}
