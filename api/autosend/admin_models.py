"""SQLAlchemy ORM layer mirroring the schema owned by storage.py.

storage.py (raw sqlite3) is the source of truth for the actual schema via
CREATE TABLE statements; the models below are ORM mappings over those same
tables for sqladmin's benefit. Base.metadata.create_all() is never called -
schema changes happen in storage.py via the rename->recreate->copy->drop
migration pattern, not here.
"""
from sqlalchemy import Column, Integer, String, Boolean, Float, ForeignKey, Table, create_engine
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import declarative_base, relationship

from autosend import crypto
from autosend.storage import DB_PATH

class EncryptedString(TypeDecorator):
    """Transparently encrypts on write / decrypts on read, so any ORM
    access (sqladmin's CRUD forms included) sees the plaintext token while
    the DB only ever stores ciphertext."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, _dialect):
        return crypto.encrypt_token(value) if value is not None else value

    def process_result_value(self, value, _dialect):
        return crypto.decrypt_token(value) if value is not None else value


Base = declarative_base()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})



class Organisation(Base):
    __tablename__ = "organisations"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    active = Column(Boolean, default=True)
    created_at = Column(String)

    units = relationship("Unit", back_populates="organisation")

    def __str__(self):
        return self.name


class Unit(Base):
    __tablename__ = "units"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    slug = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    active = Column(Boolean, default=True)

    organisation = relationship("Organisation", back_populates="units")

    # pco_token_id/pco_token_secret used to live here, per unit.
    # Moved to the per-organisation PCOOrganizationSettings table below -
    # PCO credentials are org-level, not per-unit. pco_webhook_secret
    # stays here since each unit has its own PCO webhook.
    pco_webhook_secret = Column(EncryptedString, nullable=True)
    # Purely informational free-text field - nothing in the
    # request-handling code reads this. Used to live on WhatsAppNumber,
    # but it's really a per-unit fact (who to ask about that
    # unit's PCO webhook), not per-number - moved here so it's
    # editable alongside pco_webhook_secret from the same
    # UnitWebhookAdmin page rather than on the Numbers page.
    pco_webhook_user_name = Column(String, nullable=True)
    # Nullable: a unit can be saved without a PCO campus and still
    # use the campaign sender. PCO-driven automation (registration
    # polling, form responses) just won't run for that unit until
    # one is set - see get_pco_client() in clients.py, which raises a
    # clear error at automation time rather than blocking unit
    # creation up front.
    pco_campus_id = Column(String, nullable=True)

    created_at = Column(String)

    templates = relationship("WhatsAppTemplate", back_populates="unit", cascade="all, delete-orphan")
    form_mappings = relationship("FormTemplate", back_populates="unit", cascade="all, delete-orphan")
    whatsapp_numbers = relationship("WhatsAppNumber", back_populates="unit", cascade="all, delete-orphan")

    def __str__(self):
        return self.name


class PCOOrganizationSettings(Base):
    """Per-organisation PCO API credentials - one row per org (org_id is
    NOT NULL UNIQUE, enforced in integrations/pco/schema.py). Split out
    from Unit because a PCO Personal Access Token is issued at the
    organization level, not per-unit; units only need
    their own pco_campus_id and pco_webhook_secret (still on Unit
    above)."""
    __tablename__ = "pco_organization_settings"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organisations.id"), unique=True, nullable=False)
    pco_token_id = Column(String, nullable=False)
    # nullable=True for the same SQLAdmin reason as other credential
    # columns in this file: a NOT NULL column gets a mandatory form
    # validator regardless of form_args, which would block leaving it
    # blank on *edit*. insert_model() below enforces it as required on
    # creation; update_model() keeps the existing value when left blank.
    pco_token_secret = Column(EncryptedString, nullable=True)
    created_at = Column(String)

    organisation = relationship("Organisation")

    def __str__(self):
        return "PCO Organization Settings"


class MetaPlatformSettings(Base):
    """Platform-wide Meta app credentials for WhatsApp Embedded Signup -
    singleton table, one row for the whole platform (unlike
    PCOOrganizationSettings above, which is one row per organisation).
    app_id and config_id aren't secret (already visible in the Embedded
    Signup URL itself); app_secret is a live credential, so it's
    encrypted."""
    __tablename__ = "meta_platform_settings"

    id = Column(Integer, primary_key=True)
    app_id = Column(String, nullable=False)
    # nullable=True for the same SQLAdmin reason as other credential
    # columns in this file: a NOT NULL column gets a mandatory form
    # validator regardless of form_args, blocking a blank *edit* submit.
    # insert_model() enforces it as required on creation; update_model()
    # keeps the existing value when left blank.
    app_secret = Column(EncryptedString, nullable=True)
    config_id = Column(String, nullable=False)
    # Verifies the GET /webhooks/whatsapp handshake when (re)registering
    # Meta's webhook subscription in the App Dashboard - a shared secret
    # you choose yourself, not one Meta issues. Was hardcoded directly in
    # webhooks.py before this table existed; encrypted here for
    # consistency with app_secret, even though its blast radius if
    # leaked is much smaller.
    webhook_verify_token = Column(EncryptedString, nullable=True)
    created_at = Column(String)

    def __str__(self):
        return "Meta Platform Settings"


class WhatsAppNumber(Base):
    __tablename__ = "whatsapp_numbers"

    id = Column(Integer, primary_key=True)
    unit_id = Column(Integer, ForeignKey("units.id"), nullable=False)
    label = Column(String, nullable=False)
    phone_number_id = Column(String, unique=True, nullable=False)
    # nullable=True so a number can genuinely be saved before its token is
    # on hand (see comment on waba_id below) - SQLAdmin auto-adds a
    # required-field validator for any NOT NULL column regardless of
    # form_args, so the column itself has to allow it, not just the form.
    # The DB's own NOT NULL constraint still stands (blank submits "" here,
    # never NULL), and crypto.encrypt_token("")/decrypt_token("") both
    # pass an empty string straight through unchanged.
    access_token = Column(EncryptedString, nullable=True)
    # Needed for bulk campaigns to list this number's approved templates
    # via the Graph API (POST /messages only needs phone_number_id, but
    # GET .../message_templates needs the WABA ID). Optional so a number
    # can be saved before it's on hand; the bulk-campaign UI just asks
    # for it if missing when someone tries to load templates for it.
    waba_id = Column(String, nullable=True)
    # Facebook App ID - only needed for the Resumable Upload API when
    # creating a WhatsApp template with an IMAGE header (see
    # web/templates_router.py). Optional; unrelated to waba_id/phone_number_id.
    meta_app_id = Column(String, nullable=True)
    active = Column(Boolean, default=True)
    # pco_webhook_user_name moved to Unit above - it's a
    # per-unit fact, not per-number.
    # Seconds to sleep between each message in a bulk campaign send (see
    # web/campaigns_router.py::_run_campaign). Set per-number rather than
    # per-campaign since the right pacing tracks this number's Meta quality
    # rating/tier, not something to reconsider on every campaign launch.
    send_delay_seconds = Column(Float, nullable=False, default=0.0)
    # How many sends this number holds in flight at once during a bulk
    # campaign - see web/campaign_runner.py::_run_campaign, which bounds a
    # ThreadPoolExecutor to this many workers per batch. Set per-number
    # (not per-campaign) for the same reason as send_delay_seconds: real
    # throughput tracks this number's tier/quality, not something to
    # reconsider on every campaign launch. Default 20 matches an initial
    # speed test (~10 msg/s at that batch size) - well under Meta's 20
    # msg/s coexistence ceiling, with room to raise per-number once a
    # number's real-world throughput is known.
    send_concurrency = Column(Integer, nullable=False, default=20)
    # Percent (0-100) of this number's 24h messaging-limit tier that bulk
    # campaigns should treat as their ceiling, reserving the rest for
    # transactional sends (registration/payment confirmations) - see
    # whatsapp_limits.reserve_fraction_for(). NULL means "use the app-wide
    # default" (whatsapp_limits.CAMPAIGN_RESERVE_FRACTION, currently 5%) -
    # only set this per number when a unit's registration volume
    # on that specific number needs more (or less) headroom than the
    # default reserves.
    campaign_reserve_percent = Column(Integer, nullable=True)
    # The human-readable MSISDN (e.g. "+27 82 123 4567"), as opposed to
    # phone_number_id (Meta's opaque internal ID, the only thing needed to
    # actually call the Graph API). Not a form field - it's fetched from
    # Meta automatically on every create/save of this row (see
    # WhatsAppNumberAdmin.insert_model/update_model in admin_views.py) and
    # via Embedded Signup (onboarding_router.py). NULL until the first
    # successful sync (e.g. saved without a valid access_token/
    # phone_number_id yet) or for a row that predates this column and
    # hasn't been saved or backfilled via POST /ops/sync-phone-numbers
    # since. Purely for display - nothing sends against this field.
    display_phone_number = Column(String, nullable=True)
    # 'manual' or 'embedded_signup' - purely informational/audit, nothing
    # branches on this at send time. NULL on rows that predate this column.
    onboarded_via = Column(String, nullable=True)
    created_at = Column(String)

    unit = relationship("Unit", back_populates="whatsapp_numbers")

    def __str__(self):
        return self.label


class WhatsAppTemplate(Base):
    __tablename__ = "whatsapp_templates"

    id = Column(Integer, primary_key=True)
    unit_id = Column(Integer, ForeignKey("units.id"), nullable=False)
    template_type = Column(String, nullable=False)
    template_name = Column(String, nullable=False)
    body_variable_order = Column(String, nullable=False)
    # Static literal string for the button's dynamic URL variable - replaced
    # by button_variables below (per-button field mapping instead of one
    # static pattern for the whole template). Left in place since it's still
    # a real DB column (schema.py never drops it), but nothing reads or
    # writes it anymore.
    button_url_pattern = Column(String)
    # Added to whatsapp_templates via guarded ALTER TABLE in schema.py,
    # after some deployments already had this table - not part of the
    # original CREATE TABLE. Not registered as a sqladmin ModelView (see
    # module docstring / admin.py), so this class only exists for the
    # relationship() calls on Unit/FormTemplate below; these three
    # columns were previously missing from the ORM class entirely even
    # though they're real DB columns, which is harmless only because
    # nothing ever builds a sqladmin form from this class. Kept in sync
    # with schema.py here so that stays true rather than becoming a trap
    # for whoever registers a ModelView for this later.
    header_image_url = Column(String, nullable=True)
    # Which WhatsApp number this automation sends from - informational only
    # today (see schema.py comment on this column); the actual send path
    # still always uses the unit's primary number. No FK here even
    # though SQLAlchemy could support one (unlike the ALTER TABLE in
    # schema.py, which can't add a REFERENCES clause) - left off to mirror
    # the real DB constraint (or lack of one) rather than imply an
    # enforcement that doesn't exist.
    whatsapp_number_id = Column(Integer, nullable=True)
    # JSON array parallel to the template's button list, naming which
    # available field feeds each button's {{1}} dynamic URL variable -
    # replaces button_url_pattern above (see that column's comment).
    button_variables = Column(String, nullable=True)
    active = Column(Boolean, default=True)

    unit = relationship("Unit", back_populates="templates")

    def __str__(self):
        return f"{self.template_name} ({self.template_type})"


class FormTemplate(Base):
    __tablename__ = "form_templates"

    id = Column(Integer, primary_key=True)
    unit_id = Column(Integer, ForeignKey("units.id"), nullable=False)
    pco_form_id = Column(String, nullable=False)
    whatsapp_template_id = Column(Integer, ForeignKey("whatsapp_templates.id"), nullable=False)
    active = Column(Boolean, default=True)

    unit = relationship("Unit", back_populates="form_mappings")
    whatsapp_template = relationship("WhatsAppTemplate")


user_units_table = Table(
    "user_units",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
    Column("unit_id", Integer, ForeignKey("units.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organisations.id"), nullable=True)  # NULL only for platform superadmins - see storage/schema.py
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=True)  # form-level only; insert_model/update_model enforce the real requirement
    is_superadmin = Column(Boolean, default=False)
    is_org_admin = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    created_at = Column(String)

    organisation = relationship("Organisation")
    units = relationship("Unit", secondary=user_units_table)

    def __str__(self):
        return self.username