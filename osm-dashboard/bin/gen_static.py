#!/usr/bin/env python3
"""Erzeugt einen eingefrorenen Merge-Variablen-Snapshot für den Static-Modus.

Ruft den echten Transform EINMAL lokal auf (dort funktionieren die APIs), und
schreibt das Ergebnis als kompaktes JSON. Dieses JSON kannst du dann als
`static_data` verwenden (strategy: static) -> zur Laufzeit keine API-Aufrufe,
keine Timeouts, 100% stabile Preview.

Nutzung:
    python3 bin/gen_static.py                 # nutzt Werte aus .trmnlp.yml
    python3 bin/gen_static.py --changeset_id 184732269 --map_style toner
    python3 bin/gen_static.py --write-settings   # patcht zusätzlich src/settings.yml
                                                 # (strategy: static + static_data)

Ausgabe: schreibt src/static_data.json und gibt das JSON auf stdout aus.
"""
import json, os, sys, importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_transform():
    spec = importlib.util.spec_from_file_location("transform", os.path.join(ROOT, "src", "transform.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def read_custom_fields():
    """Custom-Field-Werte aus .trmnlp.yml lesen (yaml falls vorhanden, sonst simpel)."""
    path = os.path.join(ROOT, ".trmnlp.yml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        return dict((yaml.safe_load(open(path)) or {}).get("custom_fields") or {})
    except Exception:
        cf = {}
        inblock = False
        for line in open(path, encoding="utf-8"):
            if line.rstrip() == "custom_fields:":
                inblock = True; continue
            if inblock:
                if not line.startswith("  ") or line.strip().startswith("#"):
                    if line.strip() and not line.startswith(" "): break
                    continue
                if ":" in line:
                    k, v = line.strip().split(":", 1)
                    cf[k.strip()] = v.strip().strip('"').strip("'")
        return cf

def main():
    cf = read_custom_fields()
    # CLI-Overrides: --key value
    args = sys.argv[1:]
    write_settings = "--write-settings" in args
    args = [a for a in args if a != "--write-settings"]
    # Lokal großzügiges Netzwerkbudget (kein 30s-Limit wie auf dem Server).
    budget = "150"
    if "--budget" in args:
        bi = args.index("--budget"); budget = args[bi + 1]; del args[bi:bi + 2]
    os.environ["OSM_DEADLINE_S"] = budget
    i = 0
    while i < len(args) - 1:
        if args[i].startswith("--"):
            cf[args[i][2:]] = args[i + 1]; i += 2
        else:
            i += 1

    tr = load_transform()
    res = tr.run({"trmnl": {"plugin_settings": {"custom_fields_values": cf}}})
    out = json.dumps(res, ensure_ascii=False)

    with open(os.path.join(ROOT, "src", "static_data.json"), "w", encoding="utf-8") as f:
        f.write(out)

    sys.stderr.write("custom fields : %s\n" % cf)
    sys.stderr.write("changeset     : #%s (%s)\n" % (res.get("changeset_id"), res.get("comment")))
    sys.stderr.write("error         : %r\n" % res.get("error"))
    sys.stderr.write("warn          : %r\n" % res.get("warn"))
    sys.stderr.write("snapshot bytes: %d  -> src/static_data.json\n" % len(out.encode()))

    if write_settings:
        patch_settings(out)
        sys.stderr.write("settings.yml  : strategy=static + static_data gesetzt\n")

    print(out)

def patch_settings(json_text):
    """settings.yml: strategy -> static, static_data -> EIN Block-Scalar mit dem JSON.
    Ersetzt einen evtl. vorhandenen alten static_data-Block vollständig (kein Anhängen)."""
    path = os.path.join(ROOT, "src", "settings.yml")
    lines = open(path, encoding="utf-8").read().splitlines()
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("strategy:"):
            result.append("strategy: static"); i += 1
        elif line.startswith("static_data:"):
            result.append("static_data: |")
            result.append("  " + json_text)      # Block-Scalar -> kein YAML-Escaping nötig
            i += 1
            while i < len(lines) and lines[i].startswith("  "):  # alten Block-Inhalt verwerfen
                i += 1
        else:
            result.append(line); i += 1
    open(path, "w", encoding="utf-8").write("\n".join(result) + "\n")

if __name__ == "__main__":
    main()
