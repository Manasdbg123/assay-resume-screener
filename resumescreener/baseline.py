"""
Baseline (non-LLM) scoring engine.

This is the deterministic fallback used when the Claude engine is unavailable, and
the control arm the LLM engine is measured against in `eval/evaluate.py`.

It scores by dictionary skill matching plus TF-IDF cosine similarity. That is a
genuinely weak signal - it cannot recognise "Golang" as "Go", cannot tell a skill
listed from a skill used, and rewards keyword stuffing. Those limitations are the
point: they are what the LLM engine exists to fix, and keeping this engine honest
about them is what makes the evaluation meaningful.
"""

import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .schemas import (
    DimensionScore,
    ScreeningResult,
    SkillEvidence,
    Suggestion,
    Verdict,
)
from .skills_data import (
    EDUCATION_KEYWORDS,
    EXPERIENCE_PATTERNS,
    get_all_skills,
    get_skills_by_category,
)

# Term boundaries are subtle here. Whitespace-delimited matching misses
# "PostgreSQL." at the end of a sentence; plain \b matching finds the "c" inside
# "c++" and the "go" inside "golang". These lookarounds treat + and # and an
# intra-word period as part of the term, so "node.js", "c++" and "c#" survive
# while ordinary trailing punctuation still ends a match.
_LEFT_BOUNDARY = r'(?<![\w+#/])'
_RIGHT_BOUNDARY = r'(?![\w+#]|\.\w)'


def _normalize_separators(text):
    """
    Fold hyphens to spaces so "react-native" and "react native" are the same term.

    Applied to both the document and the dictionary entries, so the two sides
    always agree on spelling.
    """
    return re.sub(r'(?<=\w)-(?=\w)', ' ', text)


def _term_pattern(term):
    """Build a boundary-anchored regex for a single skill or degree term."""
    return _LEFT_BOUNDARY + re.escape(_normalize_separators(term)) + _RIGHT_BOUNDARY


def preprocess_text(text):
    """Clean and normalize text for NLP processing."""
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove special characters but keep hyphens and periods (for tech terms)
    text = re.sub(r'[^\w\s\-\.\+\#\/]', ' ', text)
    return text.strip()


def extract_skills(text):
    """
    Extract skills from text by matching against the curated dictionary.

    Matching is longest-term-first with masking: once "react native" matches, its
    span is blanked out so the shorter "react" cannot match the same words again.
    Without this, "objective-c" would also register a bare "c", and every compound
    skill would inflate the count with its own fragments.

    Returns:
        dict with 'found_skills' (set), 'by_category' (dict), and 'total_count'
    """
    working = _normalize_separators(preprocess_text(text).lower())
    categories = get_skills_by_category()

    found_skills = set()
    # Longest first, so compound terms claim their span before their fragments.
    for skill in sorted(get_all_skills(), key=len, reverse=True):
        pattern = _term_pattern(skill)
        match = re.search(pattern, working)
        if match:
            found_skills.add(skill)
            # Blank every occurrence of this term so fragments cannot reuse it.
            working = re.sub(pattern, lambda m: " " * len(m.group(0)), working)

    found_by_category = {}
    for category, skills in categories.items():
        matched = [s for s in skills if s in found_skills]
        if matched:
            found_by_category[category] = matched

    return {
        "found_skills": found_skills,
        "by_category": found_by_category,
        "total_count": len(found_skills)
    }


def extract_experience(text):
    """
    Extract years of experience mentioned in the text.

    Returns:
        dict with 'years' (list of ints found) and 'max_years' (int)
    """
    text_lower = text.lower()
    years_found = []

    for pattern in EXPERIENCE_PATTERNS:
        matches = re.findall(pattern, text_lower)
        for match in matches:
            try:
                years = int(match)
                if 0 < years < 50:  # sanity check
                    years_found.append(years)
            except ValueError:
                continue

    return {
        "years": sorted(set(years_found), reverse=True),
        "max_years": max(years_found) if years_found else 0
    }


def extract_education(text):
    """
    Extract education qualifications from text.

    Returns:
        dict with 'degrees' (list) and 'fields' (list)
    """
    text_lower = _normalize_separators(text.lower())
    found_degrees = []
    found_fields = []

    for degree in EDUCATION_KEYWORDS["degrees"]:
        if re.search(_term_pattern(degree), text_lower):
            found_degrees.append(degree)

    for field in EDUCATION_KEYWORDS["fields"]:
        if field in text_lower:
            found_fields.append(field)

    return {
        "degrees": list(set(found_degrees)),
        "fields": list(set(found_fields))
    }


def calculate_similarity(text1, text2):
    """
    Calculate TF-IDF cosine similarity between two texts.

    Returns:
        float similarity score between 0 and 1
    """
    if not text1.strip() or not text2.strip():
        return 0.0

    try:
        vectorizer = TfidfVectorizer(
            stop_words='english',
            max_features=5000,
            ngram_range=(1, 2)
        )
        tfidf_matrix = vectorizer.fit_transform([text1, text2])
        similarity = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])
        return float(similarity[0][0])
    except Exception:
        return 0.0


def _calculate_skills_score(resume_skills, jd_skills):
    """Calculate skill match percentage."""
    if not jd_skills["found_skills"]:
        return 100.0, [], []

    matched = resume_skills["found_skills"] & jd_skills["found_skills"]
    missing = jd_skills["found_skills"] - resume_skills["found_skills"]
    extra = resume_skills["found_skills"] - jd_skills["found_skills"]

    score = (len(matched) / len(jd_skills["found_skills"])) * 100

    return min(score, 100.0), sorted(missing), sorted(extra)


def _calculate_experience_score(resume_exp, jd_exp):
    """Calculate experience match score."""
    if jd_exp["max_years"] == 0:
        # No experience requirement specified in JD
        return 80.0 if resume_exp["max_years"] > 0 else 60.0

    if resume_exp["max_years"] == 0:
        return 30.0

    ratio = resume_exp["max_years"] / jd_exp["max_years"]

    if ratio >= 1.0:
        return 100.0
    elif ratio >= 0.75:
        return 85.0
    elif ratio >= 0.5:
        return 65.0
    elif ratio >= 0.25:
        return 45.0
    else:
        return 25.0


def _calculate_education_score(resume_edu, jd_edu):
    """Calculate education match score."""
    if not jd_edu["degrees"] and not jd_edu["fields"]:
        return 75.0  # No specific requirement

    score = 0.0
    degree_weight = 60.0
    field_weight = 40.0

    # Degree matching
    if jd_edu["degrees"]:
        matched_degrees = set(resume_edu["degrees"]) & set(jd_edu["degrees"])
        if matched_degrees:
            score += degree_weight
        elif resume_edu["degrees"]:
            score += degree_weight * 0.5  # Has some degree
    else:
        score += degree_weight * 0.75

    # Field matching
    if jd_edu["fields"]:
        matched_fields = set(resume_edu["fields"]) & set(jd_edu["fields"])
        if matched_fields:
            score += field_weight
        elif resume_edu["fields"]:
            score += field_weight * 0.3
    else:
        score += field_weight * 0.75

    return min(score, 100.0)


def score_resume(resume_text, jd_text):
    """
    Master scoring function. Analyzes resume against job description
    and returns comprehensive scores.

    Args:
        resume_text: Extracted text from resume
        jd_text: Job description text

    Returns:
        dict with all scoring components and overall score
    """
    # Preprocess
    clean_resume = preprocess_text(resume_text)
    clean_jd = preprocess_text(jd_text)

    # Extract components
    resume_skills = extract_skills(clean_resume)
    jd_skills = extract_skills(clean_jd)

    resume_exp = extract_experience(clean_resume)
    jd_exp = extract_experience(clean_jd)

    resume_edu = extract_education(clean_resume)
    jd_edu = extract_education(clean_jd)

    # Calculate individual scores
    skills_score, missing_skills, extra_skills = _calculate_skills_score(
        resume_skills, jd_skills
    )
    experience_score = _calculate_experience_score(resume_exp, jd_exp)
    education_score = _calculate_education_score(resume_edu, jd_edu)
    similarity_score = calculate_similarity(clean_resume, clean_jd) * 100

    # Weighted overall score
    overall_score = (
        skills_score * 0.40 +
        similarity_score * 0.25 +
        experience_score * 0.20 +
        education_score * 0.15
    )

    # Generate suggestions
    suggestions = _generate_suggestions(
        missing_skills, skills_score, experience_score,
        education_score, similarity_score
    )

    matched_skills = sorted(resume_skills["found_skills"] & jd_skills["found_skills"])

    return {
        "overall_score": round(overall_score, 1),
        "scores": {
            "skills": round(skills_score, 1),
            "experience": round(experience_score, 1),
            "education": round(education_score, 1),
            "keyword_similarity": round(similarity_score, 1)
        },
        "details": {
            "matched_skills": matched_skills,
            "missing_skills": missing_skills,
            "extra_skills": sorted(extra_skills) if extra_skills else [],
            "resume_skills_count": resume_skills["total_count"],
            "jd_skills_count": jd_skills["total_count"],
            "resume_experience_years": resume_exp["max_years"],
            "jd_experience_years": jd_exp["max_years"],
            "resume_degrees": resume_edu["degrees"],
            "resume_fields": resume_edu["fields"],
            "jd_degrees": jd_edu["degrees"],
            "jd_fields": jd_edu["fields"],
            "skills_by_category": resume_skills["by_category"]
        },
        "suggestions": suggestions
    }


def _generate_suggestions(missing_skills, skills_score, experience_score,
                          education_score, similarity_score):
    """Generate actionable suggestions to improve resume match."""
    suggestions = []

    if missing_skills:
        top_missing = missing_skills[:5]
        suggestions.append({
            "type": "skills",
            "priority": "high",
            "message": f"Add these key skills to your resume: {', '.join(top_missing)}"
        })

    if skills_score < 50:
        suggestions.append({
            "type": "skills",
            "priority": "high",
            "message": "Your skill set has limited overlap with this role. Consider upskilling in the required technologies."
        })

    if similarity_score < 40:
        suggestions.append({
            "type": "keywords",
            "priority": "medium",
            "message": "Use more keywords from the job description in your resume. Mirror the language and terminology used by the employer."
        })

    if experience_score < 50:
        suggestions.append({
            "type": "experience",
            "priority": "medium",
            "message": "Highlight relevant experience more prominently. Quantify your achievements with metrics and numbers."
        })

    if education_score < 50:
        suggestions.append({
            "type": "education",
            "priority": "low",
            "message": "Ensure your educational qualifications are clearly listed. Include relevant certifications or courses."
        })

    if not suggestions:
        suggestions.append({
            "type": "general",
            "priority": "low",
            "message": "Great match! Your resume aligns well with this job description. Fine-tune formatting and proofread before applying."
        })

    return suggestions


# --------------------------------------------------------------------------
# Adapter: express the legacy dict scoring as the shared ScreeningResult schema
# so callers can treat both engines identically.
# --------------------------------------------------------------------------

def _verdict_for(score: float) -> Verdict:
    if score >= 80:
        return Verdict.STRONG_MATCH
    if score >= 65:
        return Verdict.MATCH
    if score >= 45:
        return Verdict.PARTIAL_MATCH
    return Verdict.WEAK_MATCH


def screen_baseline(resume_text: str, job_description: str) -> ScreeningResult:
    """Score a resume with the deterministic engine, in the shared schema."""
    raw = score_resume(resume_text, job_description)
    scores = raw["scores"]
    details = raw["details"]

    matched = details["matched_skills"]
    missing = details["missing_skills"]

    evidence = [
        SkillEvidence(
            skill=skill,
            present=True,
            evidence="Matched by dictionary lookup; the baseline engine cannot cite context.",
            strength="mentioned",
        )
        for skill in matched
    ] + [
        SkillEvidence(skill=skill, present=False, evidence="", strength="none")
        for skill in missing
    ]

    overall = raw["overall_score"]
    return ScreeningResult(
        overall_score=overall,
        verdict=_verdict_for(overall),
        summary=(
            f"Baseline keyword scoring matched {len(matched)} of "
            f"{len(matched) + len(missing)} required skills. This score reflects term "
            "overlap only, not demonstrated capability."
        ),
        skills=DimensionScore(
            score=scores["skills"],
            reasoning="Proportion of job-description skills found verbatim in the resume.",
        ),
        experience=DimensionScore(
            score=scores["experience"],
            reasoning=(
                f"Resume states {details['resume_experience_years']} years against "
                f"{details['jd_experience_years']} required."
            ),
        ),
        education=DimensionScore(
            score=scores["education"],
            reasoning="Degree and field keywords compared against the job description.",
        ),
        domain_relevance=DimensionScore(
            score=scores["keyword_similarity"],
            reasoning="TF-IDF cosine similarity between the two documents.",
        ),
        skill_evidence=evidence,
        matched_skills=matched,
        missing_skills=missing,
        transferable_skills=[],
        years_experience=float(details["resume_experience_years"]) or None,
        required_years=float(details["jd_experience_years"]) or None,
        red_flags=[],
        suggestions=[
            Suggestion(
                priority=s["priority"],
                category=s["type"] if s["type"] != "general" else "presentation",
                message=s["message"],
            )
            for s in raw["suggestions"]
        ],
    )
