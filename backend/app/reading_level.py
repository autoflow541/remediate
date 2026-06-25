"""Reading level assessment (WCAG 3.1.5 — Sprint 11).

WCAG 3.1.5 (Level AAA) recommends supplementary content for text requiring
more than lower secondary education reading ability (roughly grade 8).

This module computes the Flesch-Kincaid Grade Level for each page's body
text using syllable counting and sentence/word segmentation.

FK Grade = 0.39 × (words/sentences) + 11.8 × (syllables/words) − 15.59

Returns per-document score plus a severity assessment.
"""

from __future__ import annotations

import re


def _count_syllables(word: str) -> int:
    """Approximate syllable count for an English word."""
    word = word.lower().strip(".,!?;:\"'()[]")
    if not word:
        return 0
    # Remove silent e at end
    word = re.sub(r"e$", "", word)
    # Count vowel groups
    vowels = re.findall(r"[aeiouy]+", word)
    count = len(vowels)
    return max(1, count)


def _extract_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]


def _extract_words(text: str) -> list[str]:
    return [w for w in re.findall(r"\b[a-zA-Z]+\b", text) if len(w) > 0]


def assess_reading_level(pdf_path: str) -> dict:
    """Return a reading level assessment dict.

    Keys: grade_level, flesch_score, word_count, sentence_count,
          avg_sentence_length, severity, description
    """
    try:
        import fitz
    except ImportError:
        return {}

    text_chunks: list[str] = []
    try:
        doc = fitz.open(pdf_path)
        for page in doc:
            text_chunks.append(page.get_text("text"))
        doc.close()
    except Exception:
        return {}

    full_text = " ".join(text_chunks)
    sentences = _extract_sentences(full_text)
    words = _extract_words(full_text)

    if len(words) < 50 or len(sentences) < 5:
        return {
            "grade_level": None,
            "description": "Insufficient text for reading level assessment (< 50 words).",
        }

    total_syllables = sum(_count_syllables(w) for w in words)
    word_count = len(words)
    sentence_count = len(sentences)

    avg_words_per_sentence = word_count / sentence_count
    avg_syllables_per_word = total_syllables / word_count

    # Flesch-Kincaid Grade Level
    grade = 0.39 * avg_words_per_sentence + 11.8 * avg_syllables_per_word - 15.59
    grade = max(0, round(grade, 1))

    # Flesch Reading Ease (higher = easier)
    ease = 206.835 - 1.015 * avg_words_per_sentence - 84.6 * avg_syllables_per_word
    ease = max(0, min(100, round(ease, 1)))

    if grade <= 6:
        severity = "good"
        note = "Accessible to most readers."
    elif grade <= 8:
        severity = "ok"
        note = "Suitable for general public. Consider simplification for broader audiences."
    elif grade <= 12:
        severity = "warning"
        note = (
            "High school reading level. Consider plain-language summaries for public-facing content "
            "(WCAG 3.1.5)."
        )
    else:
        severity = "concern"
        note = (
            "Post-secondary reading level. Highly technical content. Provide a plain-language "
            "summary or glossary for general audiences (WCAG 3.1.5)."
        )

    return {
        "grade_level": grade,
        "flesch_ease": ease,
        "word_count": word_count,
        "sentence_count": sentence_count,
        "avg_sentence_length": round(avg_words_per_sentence, 1),
        "avg_syllables_per_word": round(avg_syllables_per_word, 2),
        "severity": severity,
        "description": f"Flesch-Kincaid Grade {grade} ({note})",
    }
