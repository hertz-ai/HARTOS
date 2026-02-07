"""
HevolveSocial - Request/Response Schemas
Dataclass-based schemas following the admin/schemas.py pattern.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Any


def _clean(d):
    """Remove None values from dict for JSON responses."""
    return {k: v for k, v in d.items() if v is not None}


# ─── Auth ───

@dataclass
class RegisterRequest:
    username: str
    password: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    user_type: str = 'human'

@dataclass
class LoginRequest:
    username: str
    password: str

@dataclass
class AgentRegisterRequest:
    name: str
    description: str = ''
    agent_id: Optional[str] = None  # prompt_id_flow_id


# ─── Posts ───

@dataclass
class CreatePostRequest:
    title: str
    content: str = ''
    content_type: str = 'text'
    community: Optional[str] = None
    code_language: Optional[str] = None
    media_urls: List[str] = field(default_factory=list)
    link_url: Optional[str] = None

@dataclass
class UpdatePostRequest:
    title: Optional[str] = None
    content: Optional[str] = None


# ─── Comments ───

@dataclass
class CreateCommentRequest:
    content: str
    parent_id: Optional[str] = None

@dataclass
class UpdateCommentRequest:
    content: str


# ─── Communities ───

@dataclass
class CreateCommunityRequest:
    name: str
    display_name: str = ''
    description: str = ''
    rules: str = ''
    is_private: bool = False

@dataclass
class UpdateCommunityRequest:
    display_name: Optional[str] = None
    description: Optional[str] = None
    rules: Optional[str] = None


# ─── User Profile ───

@dataclass
class UpdateProfileRequest:
    display_name: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None


# ─── Tasks ───

@dataclass
class CreateTaskRequest:
    post_id: str
    task_description: str
    required_skill: Optional[str] = None

@dataclass
class CompleteTaskRequest:
    result: str


# ─── Recipe Sharing ───

@dataclass
class ShareRecipeRequest:
    recipe_file: str
    title: str
    description: str = ''
    community: Optional[str] = None


# ─── Reports ───

@dataclass
class CreateReportRequest:
    reason: str
    details: str = ''

@dataclass
class ReviewReportRequest:
    status: str  # reviewed|resolved|dismissed


# ─── Search ───

@dataclass
class SearchRequest:
    q: str
    type: str = 'all'  # posts|comments|users|communities|all
    limit: int = 20
    offset: int = 0


# ─── API Response ───

@dataclass
class APIResponse:
    success: bool
    data: Any = None
    error: Optional[str] = None
    meta: Optional[dict] = None

    def to_dict(self):
        d = {'success': self.success}
        if self.data is not None:
            d['data'] = self.data
        if self.error is not None:
            d['error'] = self.error
        if self.meta is not None:
            d['meta'] = self.meta
        return d


# ─── Pagination ───

@dataclass
class PaginationMeta:
    page: int
    limit: int
    total: int
    has_more: bool

    def to_dict(self):
        return asdict(self)
