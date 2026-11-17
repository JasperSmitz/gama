from django import template

register = template.Library()


@register.filter
def get_item(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def as_list(value):
    return [value]


@register.filter
def pretty_json(value):
    import json
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False)
