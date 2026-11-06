# Estimate

Estimate: {{ estimate_id }}

## Scope summary

{% for area in areas %}- {{ area }}
{% endfor %}

## Priced lines

| Code | Description | Quantity | Unit | Unit price |
| --- | --- | --- | --- | --- |
{% for line in lines %}| {{ line.code or "unpriced" }} | {{ line.description }} | {{ line.quantity }} | {{ line.unit }} | {{ line.unit_price }} |
{% endfor %}

**Total:** {{ total_price }}

{% if unpriced %}## Items to be priced

{% for u in unpriced %}- {{ u }}
{% endfor %}
{% endif %}
## Exclusions

{% for e in exclusions %}- {{ e }}
{% else %}- None stated.
{% endfor %}

## Assumptions

{% for a in assumptions %}- {{ a }}
{% else %}- None stated.
{% endfor %}
