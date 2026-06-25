"""AcroForm field remediation (WCAG 4.1.2 – Name, Role, Value).

PDF forms require every interactive field to have:
  • An accessible name (/TU tooltip — announced by screen readers instead
    of the raw /T field name, which is often camelCase or db-column-style)
  • Correct role (implied by field type: /Tx, /Btn, /Ch, /Sig)
  • Required-field indication where applicable (/Ff bit 2)

Strategy:
  1. Walk /AcroForm /Fields recursively.
  2. For each field lacking /TU, derive a label from /T via slug→title.
  3. Write /TU back to the field dict.
  4. Return counts and a summary list.

Field types:
  /Tx  — text input
  /Btn — button, checkbox, or radio button
  /Ch  — choice list (listbox or combo)
  /Sig — digital signature
"""

from __future__ import annotations

import re

_ACRONYMS = frozenset(["id", "ssn", "dob", "ssid", "ein", "url", "email", "zip",
                       "po", "usa", "ada", "wcag", "pdf", "fax", "tel", "ok"])

_FIELD_TYPE_LABELS = {
    "/Tx":  "text field",
    "/Btn": "button",
    "/Ch":  "select",
    "/Sig": "signature",
}


def _slug_to_label(raw: str) -> str:
    """Convert a raw /T field name into a readable accessible label.

    Handles: camelCase, snake_case, kebab-case, dot.notation, numeric suffixes.
    Examples:
      "firstName"       → "First Name"
      "date_of_birth"   → "Date of Birth"
      "field_3"         → "Field 3"
      "emailAddress"    → "Email Address"
      "cb_agree_terms"  → "Agree Terms"
    """
    s = raw.strip()

    # Strip common prefixes that encode field type but not meaning
    for prefix in ("txt_", "tb_", "cb_", "rb_", "dd_", "btn_", "tf_", "chk_",
                   "txt", "tb", "cb", "rb"):
        if s.lower().startswith(prefix) and len(s) > len(prefix):
            s = s[len(prefix):]
            break

    # Split camelCase
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    # Replace separators with spaces
    s = re.sub(r"[_\-.]", " ", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()

    if not s:
        return raw.title()

    # Title-case each word, but upper-case known acronyms
    words = []
    for w in s.split():
        if w.lower() in _ACRONYMS:
            words.append(w.upper())
        else:
            words.append(w.capitalize())

    return " ".join(words)


def _field_type_str(ft_name: str) -> str:
    return _FIELD_TYPE_LABELS.get(ft_name, "field")


def remediate_form_fields(pdf_path: str) -> tuple[int, int, list[dict]]:
    """Add /TU accessible names to AcroForm fields that are missing them.

    Returns:
        (total_fields, fixed_count, field_summaries)
    where each summary has: name, label, type, required, fixed.
    """
    try:
        import pikepdf
    except ImportError:
        return 0, 0, []

    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)
    except Exception:
        return 0, 0, []

    acroform = pdf.Root.get("/AcroForm")
    if acroform is None:
        pdf.close()
        return 0, 0, []

    fields_arr = acroform.get("/Fields")
    if fields_arr is None:
        pdf.close()
        return 0, 0, []

    total = 0
    fixed = 0
    summaries: list[dict] = []

    def _process(field_ref):
        nonlocal total, fixed
        try:
            field = field_ref
        except Exception:
            return

        # Recurse into field groups (/Kids may contain sub-fields)
        kids = field.get("/Kids")
        if kids is not None:
            # Check if this is a widget-only kids list or a true field group.
            # True field group: kids have /T (field name).
            # Widget-only: kids are annotation widgets (no /T).
            has_subfields = False
            for kid in kids:
                try:
                    if kid.get("/T") is not None:
                        has_subfields = True
                        break
                except Exception:
                    pass
            if has_subfields:
                for kid in kids:
                    try:
                        _process(kid)
                    except Exception:
                        continue
                return

        # Leaf field — get /T (field name, required per spec)
        t_obj = field.get("/T")
        if t_obj is None:
            return  # Not a real field (probably a widget annotation)

        raw_name = str(t_obj)
        total += 1

        # Determine field type (/FT may be inherited)
        ft_obj = field.get("/FT")
        ft_name = str(ft_obj) if ft_obj is not None else "/Tx"
        type_str = _field_type_str(ft_name)

        # Check required flag (bit 2 of /Ff)
        ff_obj = field.get("/Ff")
        ff = int(ff_obj) if ff_obj is not None else 0
        is_required = bool(ff & (1 << 1))  # bit 2 (0-indexed bit 1)

        # Check if /TU already present and non-empty
        tu_obj = field.get("/TU")
        has_tu = tu_obj is not None and str(tu_obj).strip()

        label = _slug_to_label(raw_name)
        if is_required:
            label_with_req = f"{label} (required)"
        else:
            label_with_req = label

        was_fixed = False
        if not has_tu:
            try:
                field["/TU"] = pikepdf.String(label_with_req)
                was_fixed = True
                fixed += 1
            except Exception:
                pass

        summaries.append({
            "name": raw_name,
            "label": label_with_req,
            "type": type_str,
            "required": is_required,
            "fixed": was_fixed,
        })

    try:
        for f in fields_arr:
            try:
                _process(f)
            except Exception:
                continue
        pdf.save(pdf_path)
    except Exception:
        pass
    finally:
        pdf.close()

    return total, fixed, summaries
