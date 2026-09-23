"""Allowlisted legacy profile mapping. Never copy OTPs or prior ticket decisions."""


def map_legacy_profile(record):
    if isinstance(record.get("profile"), dict):
        record = record["profile"]
    aliases = {"linkedIn": "linkedin", "major": "education", "ageRange": "age"}
    profile = {}
    for key in ("firstName", "lastName", "phone", "linkedIn", "major", "company", "university"):
        value = record.get(key, record.get(aliases.get(key, ""), ""))
        if isinstance(value, str):
            profile[key] = value.strip()
    age = record.get("ageRange", record.get("age", ""))
    profile["ageRange"] = age if age in {"18-23", "24-30", "30+"} else ""
    gender = str(record.get("gender", "")).lower()
    profile["gender"] = {
        "male": "Male",
        "female": "Female",
        "prefer not to say": "Prefer not to say",
    }.get(gender, "")
    regions = {
        "beirut": "Beirut",
        "metn_baabda": "Metn/Baabda",
        "jbeil_keserwen": "Jbeil/Keserwen",
        "aley_chouf": "Aley/Chouf",
        "north": "North",
        "akkar": "Akkar",
        "south_nabatiyi": "South/Nabatiyi",
        "beqaa_hermel": "Beqaa/Hermel",
        "outside_lebanon": "Outside Lebanon",
    }
    region = str(record.get("region", ""))
    profile["region"] = regions.get(region.lower(), region if region in regions.values() else "")
    specialization = str(record.get("specialization", ""))
    names = {
        "frontend_developer": "Frontend Developer",
        "backend_developer": "Backend Developer",
        "full_stack": "Full Stack Developer",
        "other_tech": "Other in tech",
        "other_non_tech": "Other non-tech",
        "product_manager": "Product Manager",
        "ai_engineer": "AI Engineer",
        "data_scientist": "Data Scientist",
        "cloud_engineer": "Cloud Engineer",
        "devops_engineer": "DevOps Engineer",
        "mobile_developer": "Mobile Developer",
    }
    profile["specialization"] = names.get(specialization, specialization.replace("_", " ").title())
    # Map only understood values; ask the attendee to choose when old meaning is ambiguous.
    levels = {
        "student": ("Student", 0),
        "grad_student": ("Student", 2),
        "post_grad": ("Student", 4),
        "intern": ("Professional", 0),
        "<1": ("Professional", 1),
        "1-3": ("Professional", 2),
        "3-5": ("Professional", 3),
        "5-7": ("Professional", 4),
        "7+": ("Professional", 4),
    }
    selection = levels.get(str(record.get("experience", "")))
    profile["activeExpCategories"] = [selection[0]] if selection else []
    profile["expLevels"] = {"Student": 0, "Professional": 0, "Manager / Team Lead": 0}
    if selection:
        profile["expLevels"][selection[0]] = selection[1]
    profile["status"] = (
        "professional"
        if selection and selection[0] == "Professional"
        else "fresh_graduate"
        if selection == ("Student", 4)
        else "student"
    )
    return profile
