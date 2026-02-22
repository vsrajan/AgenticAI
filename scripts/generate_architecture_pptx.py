#!/usr/bin/env python3
"""Generate architecture.pptx for the Access Governance AI project.

Run:  python scripts/generate_architecture_pptx.py
Output: architecture.pptx  (in repo root)
"""

from __future__ import annotations

import os
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

# ── Palette ──────────────────────────────────────────────────────────
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
BLACK = RGBColor(0x00, 0x00, 0x00)
DARK_GRAY = RGBColor(0x33, 0x33, 0x33)
MED_GRAY = RGBColor(0x66, 0x66, 0x66)
LIGHT_GRAY = RGBColor(0xE0, 0xE0, 0xE0)
BLUE = RGBColor(0x2B, 0x57, 0x9A)
LIGHT_BLUE = RGBColor(0xD6, 0xE4, 0xF0)
DARK_BLUE = RGBColor(0x1A, 0x36, 0x5D)
GREEN = RGBColor(0x2E, 0x7D, 0x32)
LIGHT_GREEN = RGBColor(0xE8, 0xF5, 0xE9)
ORANGE = RGBColor(0xE6, 0x5C, 0x00)
LIGHT_ORANGE = RGBColor(0xFF, 0xF3, 0xE0)
PURPLE = RGBColor(0x6A, 0x1B, 0x9A)
LIGHT_PURPLE = RGBColor(0xF3, 0xE5, 0xF5)
TEAL = RGBColor(0x00, 0x69, 0x6B)
LIGHT_TEAL = RGBColor(0xE0, 0xF2, 0xF1)
RED = RGBColor(0xC6, 0x28, 0x28)

SLIDE_WIDTH = Inches(13.333)
SLIDE_HEIGHT = Inches(7.5)

OUT_PATH = Path(__file__).resolve().parent.parent / "architecture.pptx"

# ── Helpers ──────────────────────────────────────────────────────────

def _set_slide_bg(slide, color: RGBColor):
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color


def _add_box(slide, left, top, width, height, fill_color, border_color=None,
             border_width=Pt(1), corner_radius=None):
    """Add a rounded-rectangle shape and return it."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, left, top, width, height
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    ln = shape.line
    if border_color:
        ln.color.rgb = border_color
        ln.width = border_width
    else:
        ln.fill.background()
    # Adjust corner rounding (0-1 range via adjustments)
    if corner_radius is not None:
        shape.adjustments[0] = corner_radius
    else:
        shape.adjustments[0] = 0.05
    return shape


def _add_rect(slide, left, top, width, height, fill_color, border_color=None,
              border_width=Pt(1)):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, left, top, width, height
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill_color
    ln = shape.line
    if border_color:
        ln.color.rgb = border_color
        ln.width = border_width
    else:
        ln.fill.background()
    return shape


def _set_text(shape, text, font_size=Pt(12), bold=False, color=BLACK,
              alignment=PP_ALIGN.CENTER, font_name="Calibri"):
    tf = shape.text_frame
    tf.clear()
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = font_size
    p.font.bold = bold
    p.font.color.rgb = color
    p.font.name = font_name
    p.alignment = alignment
    tf.auto_size = None
    return tf


def _add_textbox(slide, left, top, width, height, text, font_size=Pt(12),
                 bold=False, color=BLACK, alignment=PP_ALIGN.LEFT,
                 font_name="Calibri"):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = font_size
    p.font.bold = bold
    p.font.color.rgb = color
    p.font.name = font_name
    p.alignment = alignment
    return tb


def _add_multiline_textbox(slide, left, top, width, height, lines,
                           font_size=Pt(11), color=BLACK, bold=False,
                           alignment=PP_ALIGN.LEFT, font_name="Calibri",
                           line_spacing=1.2):
    """Add a textbox with multiple lines, each as its own paragraph."""
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    for i, line_text in enumerate(lines):
        if i == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.text = line_text
        p.font.size = font_size
        p.font.bold = bold
        p.font.color.rgb = color
        p.font.name = font_name
        p.alignment = alignment
        p.space_after = Pt(2)
    return tb


def _add_arrow(slide, start_left, start_top, end_left, end_top,
               color=DARK_GRAY, width=Pt(2)):
    """Add a line connector (arrow) between two points."""
    connector = slide.shapes.add_connector(
        1,  # straight connector
        start_left, start_top, end_left, end_top,
    )
    connector.line.color.rgb = color
    connector.line.width = width
    # Add arrowhead at end
    connector.end_x = end_left
    connector.end_y = end_top
    return connector


def _add_arrow_shape(slide, left, top, width, height, color=BLUE):
    """Add a right-arrow shape."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RIGHT_ARROW, left, top, width, height
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def _add_chevron(slide, left, top, width, height, color=BLUE):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.CHEVRON, left, top, width, height
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


# ── Slide builders ───────────────────────────────────────────────────

def slide_title(prs):
    """Slide 1 — Title slide."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
    _set_slide_bg(slide, DARK_BLUE)

    # Title
    _add_textbox(slide, Inches(1), Inches(2), Inches(11), Inches(1.5),
                 "Access Governance AI", Pt(44), bold=True, color=WHITE,
                 alignment=PP_ALIGN.CENTER)

    # Subtitle
    _add_textbox(slide, Inches(1), Inches(3.5), Inches(11), Inches(1),
                 "Architecture Overview", Pt(28), color=RGBColor(0xBB, 0xDE, 0xFB),
                 alignment=PP_ALIGN.CENTER)

    # Description
    _add_textbox(slide, Inches(2), Inches(5), Inches(9), Inches(1),
                 "LangGraph multi-agent system with MCP tool server for documentation search, "
                 "resource discovery, and entitlement management",
                 Pt(14), color=RGBColor(0x90, 0xCA, 0xF9), alignment=PP_ALIGN.CENTER)


def slide_high_level(prs):
    """Slide 2 — High-level architecture overview."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_slide_bg(slide, WHITE)

    # Title bar
    bar = _add_rect(slide, Inches(0), Inches(0), SLIDE_WIDTH, Inches(0.9), BLUE)
    _set_text(bar, "High-Level Architecture", Pt(28), bold=True, color=WHITE)

    # ── User ─────────────────────────────────────────────────────
    user_box = _add_box(slide, Inches(0.8), Inches(2.8), Inches(1.8), Inches(1.2),
                        LIGHT_GRAY, MED_GRAY)
    _set_text(user_box, "User\n(CLI / Chat)", Pt(14), bold=True, color=DARK_GRAY)

    # Arrow user → agent
    arr1 = _add_arrow_shape(slide, Inches(2.7), Inches(3.15), Inches(0.9), Inches(0.5), BLUE)

    # ── Agent Client ─────────────────────────────────────────────
    agent_outer = _add_box(slide, Inches(3.8), Inches(1.5), Inches(4.2), Inches(4.5),
                           LIGHT_BLUE, BLUE, Pt(2))
    _set_text(agent_outer, "", Pt(1))

    _add_textbox(slide, Inches(3.9), Inches(1.6), Inches(4), Inches(0.5),
                 "Agent Client (LangGraph)", Pt(18), bold=True, color=DARK_BLUE,
                 alignment=PP_ALIGN.CENTER)

    # Router node
    router = _add_box(slide, Inches(4.4), Inches(2.2), Inches(3), Inches(0.7),
                      RGBColor(0xBB, 0xDE, 0xFB), BLUE)
    _set_text(router, "Router Node", Pt(13), bold=True, color=DARK_BLUE)

    # Knowledgebase agent
    kb = _add_box(slide, Inches(4.1), Inches(3.2), Inches(1.8), Inches(0.7),
                  LIGHT_GREEN, GREEN)
    _set_text(kb, "Knowledgebase\nAgent", Pt(11), bold=True, color=GREEN)

    # Resource agent
    res = _add_box(slide, Inches(6.2), Inches(3.2), Inches(1.5), Inches(0.7),
                   LIGHT_ORANGE, ORANGE)
    _set_text(res, "Resource\nAgent", Pt(11), bold=True, color=ORANGE)

    # Handoff
    handoff = _add_box(slide, Inches(4.9), Inches(4.2), Inches(2), Inches(0.55),
                       LIGHT_PURPLE, PURPLE)
    _set_text(handoff, "Handoff Node", Pt(11), bold=True, color=PURPLE)

    # State
    _add_textbox(slide, Inches(4.0), Inches(5.0), Inches(3.8), Inches(0.8),
                 "State: messages[] + active_agent", Pt(10), color=MED_GRAY,
                 alignment=PP_ALIGN.CENTER)

    # Arrow agent → MCP
    arr2 = _add_arrow_shape(slide, Inches(8.1), Inches(3.15), Inches(1.0), Inches(0.5), TEAL)
    _add_textbox(slide, Inches(8.1), Inches(3.7), Inches(1.0), Inches(0.5),
                 "MCP\nprotocol", Pt(9), color=TEAL, alignment=PP_ALIGN.CENTER)

    # ── MCP Server ───────────────────────────────────────────────
    mcp_outer = _add_box(slide, Inches(9.3), Inches(1.5), Inches(3.5), Inches(4.5),
                         LIGHT_TEAL, TEAL, Pt(2))
    _set_text(mcp_outer, "", Pt(1))

    _add_textbox(slide, Inches(9.4), Inches(1.6), Inches(3.3), Inches(0.5),
                 "MCP Server (FastMCP)", Pt(18), bold=True, color=TEAL,
                 alignment=PP_ALIGN.CENTER)

    # Tool groups in MCP
    kb_tools = _add_box(slide, Inches(9.6), Inches(2.3), Inches(3), Inches(0.7),
                        LIGHT_GREEN, GREEN)
    _set_text(kb_tools, "Knowledgebase Tools\nlist_topics  |  search_docs  |  read_page",
              Pt(10), color=GREEN)

    res_tools = _add_box(slide, Inches(9.6), Inches(3.2), Inches(3), Inches(1.0),
                         LIGHT_ORANGE, ORANGE)
    _set_text(res_tools, "Resource Tools\nlist_datasets | search_dataset\nfilter_dataset | "
              "filter_dataset_fuzzy\ncount_by_column | get_column_values",
              Pt(9), color=ORANGE)

    req_tools = _add_box(slide, Inches(9.6), Inches(4.4), Inches(3), Inches(0.7),
                         LIGHT_PURPLE, PURPLE)
    _set_text(req_tools, "Request Tools\nget_request_attributes | raise_entitlement_request",
              Pt(10), color=PURPLE)

    # Data sources
    _add_textbox(slide, Inches(9.5), Inches(5.3), Inches(3.2), Inches(0.7),
                 "Data: PDF docs (BM25 index) | CSV datasets | request_config.json",
                 Pt(9), color=MED_GRAY, alignment=PP_ALIGN.CENTER)

    # ── Azure OpenAI ─────────────────────────────────────────────
    llm_box = _add_box(slide, Inches(4.2), Inches(6.3), Inches(3.2), Inches(0.8),
                       RGBColor(0xE3, 0xF2, 0xFD), BLUE)
    _set_text(llm_box, "Azure OpenAI (GPT-4o)", Pt(13), bold=True, color=BLUE)

    # Arrow agent → LLM
    _add_textbox(slide, Inches(5.2), Inches(5.7), Inches(1.6), Inches(0.5),
                 "LLM calls", Pt(9), color=MED_GRAY, alignment=PP_ALIGN.CENTER)


def slide_mcp_server(prs):
    """Slide 3 — MCP Server Architecture detail."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_slide_bg(slide, WHITE)

    # Title bar
    bar = _add_rect(slide, Inches(0), Inches(0), SLIDE_WIDTH, Inches(0.9), TEAL)
    _set_text(bar, "MCP Server — Tool Architecture", Pt(28), bold=True, color=WHITE)

    # Server outer box
    srv = _add_box(slide, Inches(0.5), Inches(1.3), Inches(12.3), Inches(5.8),
                   RGBColor(0xF5, 0xF5, 0xF5), TEAL, Pt(2))
    _set_text(srv, "", Pt(1))

    _add_textbox(slide, Inches(0.7), Inches(1.4), Inches(4), Inches(0.5),
                 "MCP Server  (FastMCP + SSE/stdio)", Pt(16), bold=True, color=TEAL)

    # ── Knowledgebase Tools Group ────────────────────────────────
    y_kb = Inches(2.1)
    grp_kb = _add_box(slide, Inches(0.8), y_kb, Inches(3.6), Inches(2.8),
                      LIGHT_GREEN, GREEN, Pt(1.5))
    _set_text(grp_kb, "", Pt(1))
    _add_textbox(slide, Inches(0.9), y_kb + Inches(0.05), Inches(3.4), Inches(0.4),
                 "Knowledgebase Tools", Pt(14), bold=True, color=GREEN)

    kb_tool_info = [
        ("list_topics()", "Return all indexed topic names"),
        ("search_docs(query, topic?)", "BM25 search across PDF pages;\nreturns ranked snippets"),
        ("read_page(topic, page)", "Read full text of a specific\nPDF page by number"),
    ]
    for i, (name, desc) in enumerate(kb_tool_info):
        ty = y_kb + Inches(0.5) + Inches(i * 0.75)
        tool_box = _add_box(slide, Inches(1.0), ty, Inches(3.2), Inches(0.65),
                            WHITE, GREEN, Pt(0.75))
        tf = tool_box.text_frame
        tf.clear()
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = name
        p.font.size = Pt(11)
        p.font.bold = True
        p.font.color.rgb = DARK_GRAY
        p.font.name = "Consolas"
        p2 = tf.add_paragraph()
        p2.text = desc
        p2.font.size = Pt(9)
        p2.font.color.rgb = MED_GRAY
        p2.font.name = "Calibri"

    # Data source for KB
    ds_kb = _add_box(slide, Inches(1.0), Inches(5.2), Inches(3.2), Inches(0.6),
                     RGBColor(0xC8, 0xE6, 0xC9), GREEN, Pt(0.75))
    _set_text(ds_kb, "DocIndex  (PDF → BM25 per-page index)", Pt(9), color=GREEN)

    # ── Resource Tools Group ─────────────────────────────────────
    y_res = Inches(2.1)
    grp_res = _add_box(slide, Inches(4.7), y_res, Inches(4.0), Inches(4.1),
                       LIGHT_ORANGE, ORANGE, Pt(1.5))
    _set_text(grp_res, "", Pt(1))
    _add_textbox(slide, Inches(4.8), y_res + Inches(0.05), Inches(3.8), Inches(0.4),
                 "Resource Tools", Pt(14), bold=True, color=ORANGE)

    res_tool_info = [
        ("list_datasets()", "List available CSV dataset names"),
        ("search_dataset(dataset, query)", "Keyword search across all columns"),
        ("filter_dataset(dataset, col, val)", "Exact-match filter on a column"),
        ("filter_dataset_fuzzy(dataset, col, val, thresh?)", "Fuzzy filter (Levenshtein distance)"),
        ("count_by_column(dataset, col)", "Value frequency counts"),
        ("get_column_values(dataset, col, n?)", "Unique values in a column"),
    ]
    for i, (name, desc) in enumerate(res_tool_info):
        ty = y_res + Inches(0.5) + Inches(i * 0.58)
        tool_box = _add_box(slide, Inches(4.9), ty, Inches(3.6), Inches(0.5),
                            WHITE, ORANGE, Pt(0.75))
        tf = tool_box.text_frame
        tf.clear()
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = name
        p.font.size = Pt(10)
        p.font.bold = True
        p.font.color.rgb = DARK_GRAY
        p.font.name = "Consolas"
        p2 = tf.add_paragraph()
        p2.text = desc
        p2.font.size = Pt(8)
        p2.font.color.rgb = MED_GRAY
        p2.font.name = "Calibri"

    # Data source for resource
    ds_res = _add_box(slide, Inches(4.9), Inches(5.7), Inches(3.6), Inches(0.5),
                      RGBColor(0xFF, 0xE0, 0xB2), ORANGE, Pt(0.75))
    _set_text(ds_res, "CsvStore  (in-memory DataFrame per CSV)", Pt(9), color=ORANGE)

    # ── Request Tools Group ──────────────────────────────────────
    y_req = Inches(2.1)
    grp_req = _add_box(slide, Inches(9.0), y_req, Inches(3.5), Inches(2.8),
                       LIGHT_PURPLE, PURPLE, Pt(1.5))
    _set_text(grp_req, "", Pt(1))
    _add_textbox(slide, Inches(9.1), y_req + Inches(0.05), Inches(3.3), Inches(0.4),
                 "Request Tools", Pt(14), bold=True, color=PURPLE)

    req_tool_info = [
        ("get_request_attributes()", "Return required and optional\nfields for a request"),
        ("raise_entitlement_request(**kw)", "Submit an entitlement\naccess request (placeholder)"),
    ]
    for i, (name, desc) in enumerate(req_tool_info):
        ty = y_req + Inches(0.5) + Inches(i * 0.9)
        tool_box = _add_box(slide, Inches(9.2), ty, Inches(3.1), Inches(0.8),
                            WHITE, PURPLE, Pt(0.75))
        tf = tool_box.text_frame
        tf.clear()
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = name
        p.font.size = Pt(11)
        p.font.bold = True
        p.font.color.rgb = DARK_GRAY
        p.font.name = "Consolas"
        p2 = tf.add_paragraph()
        p2.text = desc
        p2.font.size = Pt(9)
        p2.font.color.rgb = MED_GRAY
        p2.font.name = "Calibri"

    # Data source for request
    ds_req = _add_box(slide, Inches(9.2), Inches(5.2), Inches(3.1), Inches(0.6),
                      RGBColor(0xE1, 0xBE, 0xE7), PURPLE, Pt(0.75))
    _set_text(ds_req, "request_config.json\n(dynamic param schema)", Pt(9), color=PURPLE)


def slide_agent_graph(prs):
    """Slide 4 — LangGraph Agent flow diagram."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_slide_bg(slide, WHITE)

    # Title bar
    bar = _add_rect(slide, Inches(0), Inches(0), SLIDE_WIDTH, Inches(0.9), BLUE)
    _set_text(bar, "Agent Graph — LangGraph Flow", Pt(28), bold=True, color=WHITE)

    # ── START node ───────────────────────────────────────────────
    start = _add_box(slide, Inches(0.5), Inches(2.6), Inches(1.2), Inches(0.7),
                     DARK_BLUE, DARK_BLUE)
    _set_text(start, "START", Pt(14), bold=True, color=WHITE)

    # Arrow START → route_entry decision
    _add_arrow_shape(slide, Inches(1.8), Inches(2.72), Inches(0.7), Inches(0.35), DARK_GRAY)

    # route_entry diamond
    entry = _add_box(slide, Inches(2.6), Inches(2.2), Inches(2.0), Inches(1.3),
                     RGBColor(0xFD, 0xF0, 0xD5), ORANGE, Pt(1.5))
    tf = entry.text_frame
    tf.clear()
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "route_entry()"
    p.font.size = Pt(11)
    p.font.bold = True
    p.font.color.rgb = ORANGE
    p.font.name = "Consolas"
    p.alignment = PP_ALIGN.CENTER
    p2 = tf.add_paragraph()
    p2.text = "Resume active\nspecialist or\ngo to router"
    p2.font.size = Pt(9)
    p2.font.color.rgb = MED_GRAY
    p2.alignment = PP_ALIGN.CENTER

    # Arrow → Router
    _add_arrow_shape(slide, Inches(4.7), Inches(2.72), Inches(0.7), Inches(0.35), DARK_GRAY)

    # ── Router Node ──────────────────────────────────────────────
    router = _add_box(slide, Inches(5.5), Inches(2.3), Inches(2.2), Inches(1.0),
                      RGBColor(0xBB, 0xDE, 0xFB), BLUE, Pt(2))
    tf = router.text_frame
    tf.clear()
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "Router"
    p.font.size = Pt(16)
    p.font.bold = True
    p.font.color.rgb = DARK_BLUE
    p.alignment = PP_ALIGN.CENTER
    p2 = tf.add_paragraph()
    p2.text = "LLM decides routing"
    p2.font.size = Pt(9)
    p2.font.color.rgb = MED_GRAY
    p2.alignment = PP_ALIGN.CENTER

    # route_from_router label
    _add_textbox(slide, Inches(5.5), Inches(3.35), Inches(2.2), Inches(0.5),
                 "route_from_router()", Pt(9), bold=True, color=ORANGE,
                 alignment=PP_ALIGN.CENTER, font_name="Consolas")

    # ── Knowledgebase Agent ──────────────────────────────────────
    kb_x = Inches(1.5)
    kb_y = Inches(4.3)

    kb_agent = _add_box(slide, kb_x, kb_y, Inches(2.4), Inches(1.0),
                        LIGHT_GREEN, GREEN, Pt(2))
    tf = kb_agent.text_frame
    tf.clear()
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "knowledgebase_agent"
    p.font.size = Pt(12)
    p.font.bold = True
    p.font.color.rgb = GREEN
    p.font.name = "Consolas"
    p.alignment = PP_ALIGN.CENTER
    p2 = tf.add_paragraph()
    p2.text = "Doc search specialist"
    p2.font.size = Pt(9)
    p2.font.color.rgb = MED_GRAY
    p2.alignment = PP_ALIGN.CENTER

    # Knowledgebase tools node
    kb_tools = _add_box(slide, kb_x + Inches(0.1), Inches(5.7), Inches(2.2), Inches(0.8),
                        WHITE, GREEN, Pt(1.5))
    tf = kb_tools.text_frame
    tf.clear()
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "knowledgebase_tools"
    p.font.size = Pt(11)
    p.font.bold = True
    p.font.color.rgb = GREEN
    p.font.name = "Consolas"
    p.alignment = PP_ALIGN.CENTER
    p2 = tf.add_paragraph()
    p2.text = "list_topics | search_docs\nread_page"
    p2.font.size = Pt(8)
    p2.font.color.rgb = MED_GRAY
    p2.alignment = PP_ALIGN.CENTER

    # Loop arrow label
    _add_textbox(slide, Inches(0.2), Inches(5.1), Inches(1.5), Inches(0.5),
                 "tool calls\n↕ loop", Pt(9), color=GREEN, alignment=PP_ALIGN.CENTER)

    # ── Resource Agent ───────────────────────────────────────────
    res_x = Inches(8.0)
    res_y = Inches(4.3)

    res_agent = _add_box(slide, res_x, res_y, Inches(2.4), Inches(1.0),
                         LIGHT_ORANGE, ORANGE, Pt(2))
    tf = res_agent.text_frame
    tf.clear()
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "resource_agent"
    p.font.size = Pt(12)
    p.font.bold = True
    p.font.color.rgb = ORANGE
    p.font.name = "Consolas"
    p.alignment = PP_ALIGN.CENTER
    p2 = tf.add_paragraph()
    p2.text = "Data lookup specialist"
    p2.font.size = Pt(9)
    p2.font.color.rgb = MED_GRAY
    p2.alignment = PP_ALIGN.CENTER

    # Resource tools node
    res_tools = _add_box(slide, res_x + Inches(0.1), Inches(5.7), Inches(2.2), Inches(0.8),
                         WHITE, ORANGE, Pt(1.5))
    tf = res_tools.text_frame
    tf.clear()
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "resource_tools"
    p.font.size = Pt(11)
    p.font.bold = True
    p.font.color.rgb = ORANGE
    p.font.name = "Consolas"
    p.alignment = PP_ALIGN.CENTER
    p2 = tf.add_paragraph()
    p2.text = "filter_dataset | search_dataset\ncount_by_column | + 5 more"
    p2.font.size = Pt(8)
    p2.font.color.rgb = MED_GRAY
    p2.alignment = PP_ALIGN.CENTER

    # Loop arrow label
    _add_textbox(slide, Inches(10.5), Inches(5.1), Inches(1.5), Inches(0.5),
                 "tool calls\n↕ loop", Pt(9), color=ORANGE, alignment=PP_ALIGN.CENTER)

    # ── Handoff Node ─────────────────────────────────────────────
    handoff = _add_box(slide, Inches(5.3), Inches(5.7), Inches(2.6), Inches(0.8),
                       LIGHT_PURPLE, PURPLE, Pt(2))
    tf = handoff.text_frame
    tf.clear()
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = "handoff"
    p.font.size = Pt(13)
    p.font.bold = True
    p.font.color.rgb = PURPLE
    p.font.name = "Consolas"
    p.alignment = PP_ALIGN.CENTER
    p2 = tf.add_paragraph()
    p2.text = "Clears active_agent\n→ back to Router"
    p2.font.size = Pt(9)
    p2.font.color.rgb = MED_GRAY
    p2.alignment = PP_ALIGN.CENTER

    # ── END node ─────────────────────────────────────────────────
    end_direct = _add_box(slide, Inches(5.8), Inches(1.2), Inches(1.5), Inches(0.6),
                          RED, RED)
    _set_text(end_direct, "END", Pt(14), bold=True, color=WHITE)

    _add_textbox(slide, Inches(7.5), Inches(1.2), Inches(2.5), Inches(0.6),
                 "Direct answer\n(greeting / chat)", Pt(9), color=MED_GRAY)

    # END after specialist
    end_spec = _add_box(slide, Inches(5.0), Inches(6.8), Inches(1.2), Inches(0.5),
                        RED, RED)
    _set_text(end_spec, "END", Pt(12), bold=True, color=WHITE)

    _add_textbox(slide, Inches(6.3), Inches(6.8), Inches(3.0), Inches(0.5),
                 "Specialist final answer (no tool calls)", Pt(9), color=MED_GRAY)

    # ── Conditional edge descriptions ────────────────────────────
    desc_lines = [
        "Conditional Edges:",
        "",
        "route_entry():  active_agent set → resume specialist  |  empty → router",
        "route_from_router():  route_to_knowledgebase → knowledgebase_agent  |  "
        "route_to_resource → resource_agent  |  no tool call → END",
        "specialist_edge():  has domain tool calls → tool_node  |  "
        "hand_off_to_router only → handoff  |  no tool calls → END",
    ]
    _add_multiline_textbox(slide, Inches(0.3), Inches(7.0), Inches(12.7), Inches(0.5),
                           desc_lines, Pt(8), color=DARK_GRAY, font_name="Consolas")


def slide_tool_detail(prs):
    """Slide 5 — Tool Node detail table."""
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _set_slide_bg(slide, WHITE)

    # Title bar
    bar = _add_rect(slide, Inches(0), Inches(0), SLIDE_WIDTH, Inches(0.9), DARK_BLUE)
    _set_text(bar, "Tool Bindings — Node Detail", Pt(28), bold=True, color=WHITE)

    # ── Table-like layout ────────────────────────────────────────

    # Column headers
    col_x = [Inches(0.5), Inches(3.5), Inches(8.5)]
    col_w = [Inches(2.8), Inches(4.8), Inches(4.0)]
    hdr_y = Inches(1.2)
    hdr_h = Inches(0.5)

    headers = ["Node", "Bound Tools (MCP)", "Purpose"]
    colors = [BLUE, BLUE, BLUE]
    for x, w, txt, c in zip(col_x, col_w, headers, colors):
        hdr = _add_box(slide, x, hdr_y, w, hdr_h, c, c)
        _set_text(hdr, txt, Pt(14), bold=True, color=WHITE)

    # ── Rows ─────────────────────────────────────────────────────
    rows = [
        {
            "node": "Router",
            "node_color": LIGHT_BLUE,
            "border": BLUE,
            "tools": "route_to_knowledgebase(reason)\nroute_to_resource(reason)\n\n(Internal routing tools — not MCP)",
            "purpose": "Analyzes user intent and routes to\nthe correct specialist agent.\nCan answer directly for greetings."
        },
        {
            "node": "knowledgebase_tools",
            "node_color": LIGHT_GREEN,
            "border": GREEN,
            "tools": "list_topics()\nsearch_docs(query, topic?)\nread_page(topic, page)\nhand_off_to_router(reason)",
            "purpose": "Executes documentation search\ntools from the MCP server.\nSearches BM25-indexed PDF pages."
        },
        {
            "node": "resource_tools",
            "node_color": LIGHT_ORANGE,
            "border": ORANGE,
            "tools": "list_datasets()\nsearch_dataset(dataset, query)\n"
                     "filter_dataset(dataset, col, value)\n"
                     "filter_dataset_fuzzy(dataset, col, value, thresh?)\n"
                     "count_by_column(dataset, col)\n"
                     "get_column_values(dataset, col, n?)\n"
                     "get_request_attributes()\n"
                     "raise_entitlement_request(**kwargs)\n"
                     "hand_off_to_router(reason)",
            "purpose": "Executes data lookup and request\ntools from the MCP server.\nQueries in-memory CSV DataFrames\nand manages entitlement requests."
        },
        {
            "node": "handoff",
            "node_color": LIGHT_PURPLE,
            "border": PURPLE,
            "tools": "(processes hand_off_to_router calls)",
            "purpose": "Clears active_agent state and\nreturns control to the Router\nfor re-routing to another specialist."
        },
    ]

    row_y = Inches(1.85)
    row_heights = [Inches(1.1), Inches(1.2), Inches(2.1), Inches(1.0)]

    for i, row in enumerate(rows):
        h = row_heights[i]

        # Node name cell
        cell_node = _add_box(slide, col_x[0], row_y, col_w[0], h,
                             row["node_color"], row["border"], Pt(1))
        _set_text(cell_node, row["node"], Pt(13), bold=True, color=row["border"],
                  font_name="Consolas")
        cell_node.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER

        # Tools cell
        cell_tools = _add_box(slide, col_x[1], row_y, col_w[1], h,
                              WHITE, LIGHT_GRAY, Pt(0.75))
        tf = cell_tools.text_frame
        tf.clear()
        tf.word_wrap = True
        for j, line in enumerate(row["tools"].split("\n")):
            if j == 0:
                p = tf.paragraphs[0]
            else:
                p = tf.add_paragraph()
            p.text = line
            p.font.size = Pt(10)
            p.font.name = "Consolas"
            p.font.color.rgb = DARK_GRAY
            if line.startswith("("):
                p.font.color.rgb = MED_GRAY
                p.font.italic = True

        # Purpose cell
        cell_purpose = _add_box(slide, col_x[2], row_y, col_w[2], h,
                                WHITE, LIGHT_GRAY, Pt(0.75))
        tf = cell_purpose.text_frame
        tf.clear()
        tf.word_wrap = True
        for j, line in enumerate(row["purpose"].split("\n")):
            if j == 0:
                p = tf.paragraphs[0]
            else:
                p = tf.add_paragraph()
            p.text = line
            p.font.size = Pt(10)
            p.font.color.rgb = DARK_GRAY

        row_y += h + Inches(0.1)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    prs = Presentation()
    prs.slide_width = SLIDE_WIDTH
    prs.slide_height = SLIDE_HEIGHT

    slide_title(prs)
    slide_high_level(prs)
    slide_mcp_server(prs)
    slide_agent_graph(prs)
    slide_tool_detail(prs)

    prs.save(str(OUT_PATH))
    print(f"Generated {OUT_PATH}")


if __name__ == "__main__":
    main()
