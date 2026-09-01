import hashlib

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas, crypto
from ..ai.base import get_provider
from ..auth import get_current_user
from ..database import get_db
from ..permissions import assert_can_view_report

router = APIRouter(prefix="/chat", tags=["chat"])

_SYSTEM = (
    "You are an analyst assistant inside an internal vulnerability-reporting "
    "dashboard. You receive one or more markdown reports produced by AI "
    "coding/security tools and answer the user's questions about them.\n\n"
    "Output rules — these matter because your reply may be saved as a file:\n"
    "- If the user asks for a CSV or spreadsheet, respond with RAW CSV ONLY "
    "(comma-separated, newline rows, a header row, RFC4180 quoting for fields "
    "that contain commas or newlines). DO NOT wrap it in a markdown table or "
    "code fence.\n"
    "- If the user asks for JSON, respond with raw JSON only.\n"
    "- Otherwise, respond with a complete, well-structured Markdown document "
    "(headings, tables, lists).\n"
    "- Never preface a CSV/JSON response with 'Here is the spreadsheet:' — "
    "output the file contents directly so they can be saved as-is."
)


@router.post("", response_model=schemas.ChatResponse)
def chat(
    body: schemas.ChatRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    # load / create session
    if body.session_id:
        session = db.get(models.ChatSession, body.session_id)
        if not session or session.user_id != user.id:
            raise HTTPException(404, "Session not found")
    else:
        session = models.ChatSession(user_id=user.id, title=body.message[:60] or "Chat")
        db.add(session)
        db.commit()
        db.refresh(session)

    # gather referenced report contents (respecting RBAC)
    ctx_chunks: list[str] = []
    for rid in body.report_ids:
        rpt = db.get(models.Report, rid)
        if not rpt:
            continue
        assert_can_view_report(db, user, rpt)
        text = crypto.decrypt(rpt.content_enc).decode("utf-8", errors="replace")
        ctx_chunks.append(f"### Report: {rpt.filename} ({rpt.source_tool.value})\n\n{text}")

    history = [
        {"role": m.role, "content": m.content}
        for m in session.messages
        if m.role in ("user", "assistant")
    ]
    user_turn = body.message
    if ctx_chunks:
        user_turn = (
            "Context reports:\n\n"
            + "\n\n---\n\n".join(ctx_chunks)
            + "\n\n---\n\nUser question:\n"
            + body.message
        )
    history.append({"role": "user", "content": user_turn})

    from ..ai import scope
    choice = scope.resolve(db, user_id=user.id)
    provider = get_provider(choice.provider, choice.model)
    reply = provider.chat(_SYSTEM, history)

    db.add(models.ChatMessage(session_id=session.id, role="user", content=body.message))
    db.add(models.ChatMessage(session_id=session.id, role="assistant", content=reply))

    generated_id = None
    if body.save_as_report:
        fname = (body.save_filename or f"generated-{session.id[:8]}.md").strip()
        # Add a default .md only when the filename has no extension at all.
        # CSV / JSON / XML / etc. pass through unchanged.
        if "." not in fname:
            fname = fname + ".md"
        raw = reply.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        rpt = models.Report(
            user_id=user.id,
            agent_id=None,
            source_tool=models.SourceTool.generated,
            filename=fname,
            original_path=None,
            sha256=sha,
            size_bytes=len(raw),
            content_enc=crypto.encrypt(raw),
        )
        db.add(rpt)
        db.flush()
        generated_id = rpt.id

    db.commit()
    return schemas.ChatResponse(session_id=session.id, reply=reply, generated_report_id=generated_id)
