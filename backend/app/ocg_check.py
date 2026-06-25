"""Optional content group (OCG / layer) accessibility check — Sprint 24 (PDF/UA 7.11).

PDF/UA-1 §7.11: if a document uses Optional Content (layers), every OCG must
have a /Name, and the default visibility state must not hide content that is
required for understanding the document.

This module:
  1. Lists all OCGs and their default state (ON / OFF)
  2. Flags OCGs with missing names
  3. Warns about OCGs that are OFF by default (content may be hidden from AT)
  4. Warns about OCGs whose /Print state is OFF (won't appear in print output)
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def check_optional_content(pdf_path: str) -> list[dict]:
    """Inspect OCG metadata for accessibility issues.

    Returns a list of issue dicts:
      {type, severity, layer (optional), description}
    """
    issues: list[dict] = []

    try:
        import pikepdf
        pdf = pikepdf.open(pdf_path)
    except Exception as exc:
        log.debug("ocg_check open: %s", exc)
        return issues

    try:
        oc_props = pdf.Root.get("/OCProperties")
        if oc_props is None:
            return issues  # No optional content — nothing to check

        ocgs = oc_props.get("/OCGs") or []
        d = oc_props.get("/D")   # default viewing configuration

        # Collect names of groups that are OFF by default
        off_names: set[str] = set()
        if d:
            off_list = d.get("/OFF") or []
            for ref in off_list:
                try:
                    obj = ref.get_object() if hasattr(ref, "get_object") else ref
                    name = str(obj.get("/Name", "")).strip()
                    if name:
                        off_names.add(name)
                except Exception:
                    pass

        for ocg_ref in ocgs:
            try:
                ocg = ocg_ref.get_object() if hasattr(ocg_ref, "get_object") else ocg_ref
                name = str(ocg.get("/Name", "")).strip()

                if not name:
                    issues.append({
                        "type": "ocg_no_name",
                        "severity": "warning",
                        "description": "An optional content group (layer) has no /Name — "
                                       "it cannot be identified by assistive technology.",
                    })
                    continue

                if name in off_names:
                    issues.append({
                        "type": "ocg_hidden_by_default",
                        "severity": "warning",
                        "layer": name,
                        "description": (
                            f"Layer '{name}' is hidden by default. If it contains "
                            "meaningful tagged content, screen readers may not see it."
                        ),
                    })

                usage = ocg.get("/Usage")
                if usage:
                    print_usage = usage.get("/Print")
                    if print_usage:
                        state = str(print_usage.get("/PrintState", "")).strip("/")
                        if state == "OFF":
                            issues.append({
                                "type": "ocg_print_off",
                                "severity": "info",
                                "layer": name,
                                "description": (
                                    f"Layer '{name}' is excluded from print output. "
                                    "Verify this layer is decorative and does not "
                                    "carry meaning required for understanding."
                                ),
                            })
            except Exception:
                pass
    except Exception as exc:
        log.debug("ocg_check: %s", exc)
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    return issues
