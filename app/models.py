import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
RequiredText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
Category = Literal["Student", "Professional", "Manager / Team Lead"]
Region = Literal[
    "Beirut",
    "Metn/Baabda",
    "Jbeil/Keserwen",
    "Aley/Chouf",
    "North",
    "Akkar",
    "South/Nabatiyi",
    "Beqaa/Hermel",
    "Outside Lebanon",
]
Takeaway = Literal[
    "New technologies",
    "Networking",
    "Hands-on workshops",
    "Inspiring speakers",
    "Local tech community",
    "Career opportunities",
    "Open Source challenge",
    "Other",
]
Technology = Literal[
    "Front End",
    "Cloud",
    "Kubernetes",
    "Microservices",
    "Databases",
    "Android",
    "Flutter",
    "Machine Learning",
    "Tensorflow / Keras",
    "Google Gemini / ChatGPT",
    "Cybersecurity",
    "Web3 / Blockchain",
    "Other",
]


def normalize_email(value: str) -> str:
    return value.strip().lower()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegistrationForm(StrictModel):
    # Camel-case deliberately matches src/App.jsx.
    email: EmailStr
    firstName: RequiredText
    lastName: RequiredText
    linkedIn: Annotated[str, StringConstraints(max_length=500)]
    phone: Text = ""
    region: Region
    ageRange: Literal["", "18-23", "24-30", "30+"] = ""
    gender: Literal["", "Male", "Female", "Prefer not to say"] = ""
    specialization: RequiredText
    activeExpCategories: list[Category] = Field(min_length=1, max_length=3)
    expLevels: dict[Category, Annotated[int, Field(strict=True, ge=0)]]
    status: Text = ""
    company: Text = ""
    university: Text = ""
    major: Text = ""
    referral: RequiredText
    referralOtherText: Text = ""
    referralPartnerText: Text = ""
    attendedBefore: int = Field(ge=0, le=3, strict=True)
    takeaways: list[Takeaway] = Field(min_length=1, max_length=8)
    techInterests: list[Technology] = Field(default_factory=list, max_length=13)
    otherTakeawaysInput: Text = ""
    otherTechInterestInput: Text = ""
    comments: Annotated[str, StringConstraints(max_length=5000)] = ""
    attendanceType: Literal["full_day", "few_hours", "networking", "afternoon"]
    secretCode: Annotated[str, StringConstraints(max_length=200)] = Field(default="", repr=False)

    @model_validator(mode="after")
    def validate_form(self):
        self.email = normalize_email(str(self.email))
        url = urlsplit(self.linkedIn)
        if (
            url.scheme not in {"http", "https"}
            or url.hostname not in {"linkedin.com", "www.linkedin.com"}
            or not url.path.strip("/")
            or url.username
            or url.password
        ):
            raise ValueError("Provide a valid LinkedIn profile URL")
        self.phone = re.sub(r"\s+", "", self.phone)
        if self.phone and not re.fullmatch(r"(?:\+961)?(?:03|71|76|78|79)\d{6}", self.phone):
            raise ValueError("Invalid Lebanese phone number")
        if not self.company and not self.university:
            raise ValueError("Company or university is required")
        for key, limit in {"Student": 7, "Professional": 4, "Manager / Team Lead": 5}.items():
            if key in self.expLevels and self.expLevels[key] > limit:
                raise ValueError(f"Invalid experience level for {key}")
        if any(key not in self.expLevels for key in self.activeExpCategories):
            raise ValueError("Provide a level for each selected experience category")
        for values in (self.activeExpCategories, self.takeaways, self.techInterests):
            if len(values) != len(set(values)):
                raise ValueError("Selections must not contain duplicates")
        if {"Professional", "Manager / Team Lead"} & set(self.activeExpCategories):
            self.status = "professional"
        elif self.expLevels["Student"] in {4, 6}:
            self.status = "fresh_graduate"
        else:
            self.status = "student"
        if self.status == "student" and not self.major:
            raise ValueError("Major is required for students")
        return self


class RegistrationRequest(StrictModel):
    email: EmailStr
    form: RegistrationForm

    @model_validator(mode="after")
    def matching_emails(self):
        self.email = normalize_email(str(self.email))
        if self.email != self.form.email:
            raise ValueError("Form email must match registration email")
        return self


class UpdateRegistrationRequest(RegistrationRequest):
    version: int = Field(ge=1, strict=True)


class VersionRequest(StrictModel):
    version: int = Field(ge=1, strict=True)


class ConfirmRequest(StrictModel):
    token: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)


class CurationRequest(VersionRequest):
    ticketId: str = Field(pattern=r"^[a-f0-9]{64}$")
    action: Literal["invite", "waitlist", "reject"]
    requestId: str = Field(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class CheckInRequest(StrictModel):
    qr: str = Field(min_length=20, max_length=400)


class Identity(BaseModel):
    uid: str
    email: str
    organizer: bool = False


class EmailLookupRequest(StrictModel):
    email: EmailStr


class OTPVerifyRequest(StrictModel):
    challengeId: str = Field(pattern=r"^[a-f0-9]{32}$")
    code: str = Field(pattern=r"^\d{6}$", repr=False)


class UnverifiedRegistrationRequest(RegistrationRequest):
    submissionKey: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    # UI state is accepted for clarity, but it never determines server verification.
    emailVerified: bool = False


class CompletePendingRequest(StrictModel):
    id: str = Field(pattern=r"^[a-f0-9]{64}$")


class EmailVerificationRequest(EmailLookupRequest):
    pendingId: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class EmailVerificationCompleteRequest(StrictModel):
    code: str = Field(min_length=1, max_length=2048, repr=False)
