#!/usr/bin/env python3
"""Validate schema.yaml against the Google Cloud Marketplace v2 schema rules.

Producer Portal extracts /data/schema.yaml from the deployer image and renders the
deployment form from it. A schema it cannot parse blocks the "Specify deployment
container images" step, so this checks the documented rules before anything is
pushed. Runs offline - no Docker, no registry, no cluster.

Rules encoded here come from:
  https://github.com/GoogleCloudPlatform/marketplace-k8s-app-tools/blob/master/docs/schema.md
  https://docs.cloud.google.com/marketplace/docs/partners/kubernetes/create-app-package
"""

import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip3 install pyyaml")

# x-google-marketplace.type values accepted on a property, per schema.md.
VALID_TYPES = {
    "NAME", "NAMESPACE", "MASKED_FIELD", "GENERATED_PASSWORD", "SERVICE_ACCOUNT",
    "STORAGE_CLASS", "STRING", "APPLICATION_UID", "ISTIO_ENABLED",
    "INGRESS_AVAILABLE", "TLS_CERTIFICATE", "DEPLOYER_IMAGE", "REPORTING_SECRET",
}
VALID_IMAGE_TYPES = {
    "FULL", "REGISTRY", "REPO_WITH_REGISTRY", "REPO_WITHOUT_REGISTRY", "TAG",
}
# The only HTML tags permitted inside a form help widget.
ALLOWED_HTML = {"a", "h2", "h3", "p", "b", "i", "u", "em"}

errors, warnings = [], []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def load(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def nested_get(values, dotted):
    """Resolve a dotted schema property name against the chart's values.yaml."""
    node = values
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def validate(schema_path, values_path, chart_path):
    schema = load(schema_path)
    values = load(values_path) if values_path else None
    chart = load(chart_path) if chart_path else None

    xgm = schema.get("x-google-marketplace")
    if not xgm:
        err("missing top-level x-google-marketplace")
        return

    if xgm.get("schemaVersion") != "v2":
        err(f"x-google-marketplace.schemaVersion must be 'v2', got {xgm.get('schemaVersion')!r}")
    if not xgm.get("applicationApiVersion"):
        err("x-google-marketplace.applicationApiVersion is required")

    published = xgm.get("publishedVersion")
    if not published:
        err("x-google-marketplace.publishedVersion is required")
    elif not re.fullmatch(r"\d+\.\d+\.\d+", str(published)):
        warn(f"publishedVersion {published!r} is not MAJOR.MINOR.PATCH; "
             "Marketplace expects Semantic Versioning")

    meta = xgm.get("publishedVersionMetadata") or {}
    if not meta.get("releaseNote"):
        err("x-google-marketplace.publishedVersionMetadata.releaseNote is required")

    # publishedVersion must agree with the chart, or the portal ships a schema
    # describing a version the chart does not build.
    if chart and published and str(chart.get("appVersion")) != str(published):
        err(f"publishedVersion {published!r} != chart appVersion "
            f"{chart.get('appVersion')!r} - these must match")

    props = schema.get("properties") or {}
    required = schema.get("required") or []

    # NAME and NAMESPACE are mandatory, and must be in required.
    by_type = {}
    for name, spec in props.items():
        t = ((spec or {}).get("x-google-marketplace") or {}).get("type")
        if t:
            by_type.setdefault(t, []).append(name)
            if t not in VALID_TYPES:
                err(f"property {name!r}: unknown x-google-marketplace.type {t!r}")
    for needed in ("NAME", "NAMESPACE"):
        names = by_type.get(needed, [])
        if not names:
            err(f"no property declares x-google-marketplace.type {needed} (mandatory)")
        for n in names:
            if n not in required:
                err(f"property {n!r} (type {needed}) must be listed in required")

    # Everything in required must actually be defined.
    for r in required:
        if r not in props:
            err(f"required lists {r!r}, which is not defined in properties")

    # Images block.
    images = xgm.get("images")
    if not images:
        err("x-google-marketplace.images is required")
    else:
        for img_name, img in images.items():
            label = "primary ('')" if img_name == "" else f"{img_name!r}"
            iprops = (img or {}).get("properties") or {}
            if not iprops:
                err(f"image {label}: no properties block")
            for pname, pspec in iprops.items():
                ptype = (pspec or {}).get("type")
                if ptype not in VALID_IMAGE_TYPES:
                    err(f"image {label} property {pname!r}: bad type {ptype!r}")
                # The property must exist in the chart, or the substitution
                # silently lands nowhere.
                if values is not None:
                    _, found = nested_get(values, pname)
                    if not found:
                        err(f"image {label} property {pname!r} does not exist in "
                            f"the chart's values.yaml")

    # Every non-image property must map to a real chart value too.
    if values is not None:
        skip = set(by_type.get("NAME", [])) | set(by_type.get("NAMESPACE", []))
        for name in props:
            if name in skip:
                continue
            _, found = nested_get(values, name)
            if not found:
                err(f"property {name!r} does not exist in the chart's values.yaml")

    # Form: at most one help widget, restricted HTML.
    form = schema.get("form")
    if form is not None:
        if not isinstance(form, list):
            err("form must be a list")
        else:
            helps = [f for f in form if (f or {}).get("widget") == "help"]
            if len(helps) > 1:
                err(f"only a single help widget is allowed, found {len(helps)}")
            for f in form:
                if (f or {}).get("widget") != "help":
                    err(f"unsupported form widget {(f or {}).get('widget')!r}")
                for tag in re.findall(r"</?([a-zA-Z0-9]+)", str((f or {}).get("description", ""))):
                    if tag.lower() not in ALLOWED_HTML:
                        err(f"form help uses disallowed HTML tag <{tag}>; "
                            f"allowed: {', '.join(sorted(ALLOWED_HTML))}")

    print(f"  {len(props)} properties, {len(required)} required, "
          f"{len(images or {})} image(s)")


# x-google-marketplace types the verification harness fills in by itself.
AUTO_FILLED = {
    "NAME", "NAMESPACE", "REPORTING_SECRET", "GENERATED_PASSWORD",
    "APPLICATION_UID", "ISTIO_ENABLED", "INGRESS_AVAILABLE", "DEPLOYER_IMAGE",
}


def validate_merged(schema_path, overlay_path):
    """Check the schema Marketplace verification actually deploys.

    Verification runs unattended: it merges the test overlay over the main schema
    (overlay_test_schema.py) and then rejects the deploy if any required property
    has no value. So every required property must carry a default or be one of
    the types the harness fills in. Getting this wrong fails only in Google's
    verification, never locally, so it is worth checking here.
    """
    main = load(schema_path)
    overlay = load(overlay_path) if overlay_path else {}

    def xtype(doc, prop):
        spec = (doc.get("properties") or {}).get(prop) or {}
        return (spec.get("x-google-marketplace") or {}).get("type")

    merged = dict(main)
    merged["properties"] = dict(main.get("properties") or {})
    for prop in (overlay.get("properties") or {}):
        # overlay_test_schema.py refuses to change a property's type
        if xtype(overlay, prop) != xtype(main, prop):
            err(f"overlay changes x-google-marketplace type of {prop!r}: "
                f"{xtype(overlay, prop)!r} vs {xtype(main, prop)!r} — "
                "the deployer rejects this")
        merged["properties"][prop] = overlay["properties"][prop]

    unsatisfied = []
    for prop in merged.get("required") or []:
        spec = merged["properties"].get(prop) or {}
        if "default" not in spec and xtype(merged, prop) not in AUTO_FILLED:
            unsatisfied.append(prop)
    for prop in unsatisfied:
        err(f"required property {prop!r} has no default and is not auto-filled — "
            "Marketplace verification will fail with MissingRequiredProperty. "
            "Give it a test default in the apptest overlay.")

    print(f"  merged: {len(merged['properties'])} properties, "
          f"{len(merged.get('required') or [])} required, "
          f"{len(unsatisfied)} unsatisfied")


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: validate-schema.py <schema.yaml> [values.yaml] [Chart.yaml] [--overlay <path>]")
    argv = sys.argv[1:]
    overlay_path = None
    if "--overlay" in argv:
        i = argv.index("--overlay")
        overlay_path = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]

    schema_path = argv[0]
    values_path = argv[1] if len(argv) > 1 else None
    chart_path = argv[2] if len(argv) > 2 else None

    print(f"validating {schema_path}")
    validate(schema_path, values_path, chart_path)
    if overlay_path:
        print(f"  + overlay {overlay_path} (as Marketplace verification merges it)")
        validate_merged(schema_path, overlay_path)

    for w in warnings:
        print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")
    if errors:
        sys.exit(f"\n{len(errors)} error(s) - Producer Portal would reject this schema")
    print("  OK")


if __name__ == "__main__":
    main()
