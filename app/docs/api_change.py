from pathlib import Path

import markdown
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/changelog", response_class=HTMLResponse)
def get_api_changelog() -> str:
    changelog_path = Path("app/docs") / "api-changelog.md"
    with changelog_path.open(encoding="utf-8") as f:
        md_content = f.read()
    html_content = markdown.markdown(md_content, extensions=["tables", "fenced_code"])
    return f"""
    <html>
      <head>
        <title>API Changelog</title>
        <style>
          body {{
            max-width: 800px;
            margin: 2rem auto;
            font-family: sans-serif;
            line-height: 1.6;
          }}
          h2, h3 {{
            color: #333;
          }}
          pre {{
            background: #f4f4f4;
            padding: 1rem;
            border-radius: 6px;
          }}
          code {{
            background: #f0f0f0;
            padding: 0.2rem 0.4rem;
            border-radius: 4px;
          }}
        </style>
      </head>
      <body>
        {html_content}
      </body>
    </html>
    """
