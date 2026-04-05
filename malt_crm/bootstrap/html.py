"""Pages HTML du flux de démarrage (settings + progression)."""

from __future__ import annotations

import html


def render_settings_html(
    *,
    error_message: str | None,
    remember_placeholder: str,
    openai_placeholder: str,
) -> str:
    error_html = (
        f'<p class="error">{html.escape(error_message)}</p>' if error_message else ""
    )
    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Malt Inbox · Settings</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3f2ee;
      --panel: #ffffff;
      --line: #e5e0d5;
      --ink: #1b1a16;
      --muted: #6f6a5f;
      --primary: #171717;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: var(--bg);
      color: var(--ink);
      font-family: "Avenir Next", "Helvetica Neue", sans-serif;
      padding: 20px;
    }}
    .card {{
      width: min(620px, 100%);
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel);
      padding: 22px;
      box-shadow: 0 8px 26px rgba(20, 20, 20, 0.04);
    }}
    h1 {{
      margin: 0;
      font-size: 26px;
      letter-spacing: -0.02em;
    }}
    p {{
      margin: 10px 0 0;
      color: var(--muted);
      line-height: 1.5;
    }}
    form {{
      margin-top: 18px;
      display: grid;
      gap: 12px;
    }}
    label {{
      display: grid;
      gap: 6px;
      font-size: 13px;
      color: var(--muted);
    }}
    input {{
      width: 100%;
      min-height: 44px;
      border: 1px solid #d7d2c7;
      border-radius: 12px;
      padding: 0 12px;
      font: inherit;
      color: var(--ink);
      background: #fcfbf9;
    }}
    button {{
      min-height: 44px;
      border: 1px solid var(--primary);
      border-radius: 12px;
      background: var(--primary);
      color: #fff;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }}
    .hint {{
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
    }}
    .error {{
      min-height: 20px;
      font-size: 13px;
      color: #a03030;
    }}
  </style>
</head>
<body>
  <main class="card">
    <h1>Settings initiaux</h1>
    <p>Renseigne ton cookie <code>remember-me</code>. La clé OpenAI est optionnelle.</p>
    {error_html}
    <form id="settingsForm">
      <label for="rememberMe">Cookie remember-me (Malt)</label>
      <input id="rememberMe" name="rememberMe" autocomplete="off" placeholder="{remember_placeholder}" />
      <label for="openaiApiKey">Clé OPENAI_API_KEY</label>
      <input id="openaiApiKey" name="openaiApiKey" autocomplete="off" placeholder="{openai_placeholder}" />
      <button type="submit">Enregistrer et continuer</button>
      <div class="error" id="formError"></div>
    </form>
    <p class="hint">Les valeurs sont stockées localement dans <code>.env</code>.</p>
  </main>
  <script>
    const form = document.getElementById("settingsForm");
    const errorNode = document.getElementById("formError");
    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      errorNode.textContent = "";
      const rememberMe = document.getElementById("rememberMe").value.trim();
      const openaiApiKey = document.getElementById("openaiApiKey").value.trim();
      if (!rememberMe) {{
        errorNode.textContent = "Le cookie remember-me est obligatoire.";
        return;
      }}
      try {{
        const response = await fetch("/api/settings", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ remember_me: rememberMe, openai_api_key: openaiApiKey }}),
        }});
        const payload = await response.json().catch(() => ({{}}));
        if (!response.ok) {{
          throw new Error(payload.error || "Impossible d'enregistrer les settings.");
        }}
        document.body.innerHTML = '<main class="card"><h1>Configuration enregistrée</h1><p>Initialisation en cours…</p></main>';
        const waitForProgress = async () => {{
          try {{
            const probe = await fetch("/api/progress", {{ cache: "no-store" }});
            if (probe.ok) {{
              window.location.href = "/progress";
              return;
            }}
          }} catch (error) {{
            // Progress server not ready yet.
          }}
          window.setTimeout(waitForProgress, 500);
        }};
        window.setTimeout(waitForProgress, 500);
      }} catch (error) {{
        errorNode.textContent = error.message || "Erreur inconnue.";
      }}
    }});
  </script>
</body>
</html>
"""


def render_progress_html() -> str:
    return """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Malt Inbox · Synchronisation</title>
  <style>
    :root { --bg:#f3f2ee; --panel:#fff; --line:#e5e0d5; --ink:#1b1a16; --muted:#6f6a5f; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); font-family:"Avenir Next","Helvetica Neue",sans-serif; color:var(--ink); padding:20px; }
    .card { width:min(680px,100%); border:1px solid var(--line); border-radius:18px; background:var(--panel); padding:22px; display:grid; gap:14px; }
    h1 { margin:0; font-size:28px; letter-spacing:-0.02em; }
    p { margin:0; color:var(--muted); }
    .bar-wrap { width:100%; height:12px; border-radius:999px; background:#ece7dc; overflow:hidden; }
    .bar { height:100%; width:0%; background:#171717; transition:width 220ms ease; }
    .stage { font-size:14px; font-weight:600; }
    .detail { color:var(--muted); font-size:13px; min-height:20px; }
    .ok { color:#2f7a48; }
    .err { color:#a03030; }
  </style>
</head>
<body>
  <main class="card">
    <h1>Initialisation en cours</h1>
    <p>L'app prépare ta messagerie locale.</p>
    <div class="bar-wrap"><div id="bar" class="bar"></div></div>
    <div id="stage" class="stage">Démarrage…</div>
    <div id="detail" class="detail">Préparation du service de synchronisation.</div>
  </main>
  <script>
    const bar = document.getElementById("bar");
    const stage = document.getElementById("stage");
    const detail = document.getElementById("detail");
    const labels = { sync:"Initialisation", profile:"Profil", conversations:"Conversations", messages:"Messages", ai:"Analyse IA", done:"Terminé" };
    async function tick() {
      try {
        const response = await fetch("/api/progress", { cache: "no-store" });
        const payload = await response.json();
        bar.style.width = `${Math.max(0, Math.min(100, payload.percent || 10))}%`;
        stage.textContent = labels[payload.stage] || "Initialisation";
        detail.textContent = payload.detail || "";
        detail.className = payload.status === "error" ? "detail err" : (payload.done ? "detail ok" : "detail");
        if (payload.done && payload.redirect_url && payload.status !== "error") {
          window.location.href = payload.redirect_url;
          return;
        }
      } catch (error) {
        detail.textContent = "Connexion en cours…";
      }
      window.setTimeout(tick, 800);
    }
    tick();
  </script>
</body>
</html>
"""


PROGRESS_PAGE_BYTES = render_progress_html().encode("utf-8")
