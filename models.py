from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


# ========== COMPANY / MULTI-TENANT ==========
class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    address = db.Column(db.Text)
    subscription_plan = db.Column(db.String(50), default='basic')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    users = db.relationship('User', backref='company', lazy=True)
    inventory_items = db.relationship('InventoryItem', backref='company', lazy=True)
    categories = db.relationship('Category', backref='company', lazy=True)
    suppliers = db.relationship('Supplier', backref='company', lazy=True)


# ========== USERS ==========
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20))
    role = db.Column(db.String(50), default='user')  # admin, manager, user
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


# ========== CATEGORIES ==========
class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    icon = db.Column(db.String(50))
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def get_predefined_categories():
        return [
            'Air Conditioner',
            'Refrigerator',
            'Washing Machine',
            'Water Purifier',
            'Microwave/Oven',
            'TV/Entertainment',
            'Geyser/Water Heater',
            'Inverter/Battery',
            'Kitchen Chimney',
            'Vacuum Cleaner',
            'Furniture',
            'Plumbing',
            'Electrical',
            '2 Wheeler',       # Vehicle — triggers Fuel Log tab
            '4 Wheeler',       # Vehicle — triggers Fuel Log tab
            'Other Appliance'
        ]

    @property
    def is_vehicle(self):
        """True when this category should show the Fuel Log tab."""
        return self.name in ('2 Wheeler', '4 Wheeler')


# ========== SUPPLIER ==========
class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    contact_person = db.Column(db.String(100))
    email = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    alternative_phone = db.Column(db.String(20))
    address = db.Column(db.Text)
    gst_number = db.Column(db.String(50))
    service_type = db.Column(db.String(100))
    payment_terms = db.Column(db.String(100))
    rating = db.Column(db.Integer, default=3)
    notes = db.Column(db.Text)

    # Location fields
    main_location = db.Column(db.String(100))
    sub_location = db.Column(db.String(100))

    # Installation details
    installation_date = db.Column(db.DateTime)
    installation_by = db.Column(db.String(100))
    installation_ref_no = db.Column(db.String(100))
    installation_notes = db.Column(db.Text)
    warranty_card_no = db.Column(db.String(100))
    installation_photo = db.Column(db.String(500))      # original photo path
    installation_certificate = db.Column(db.String(500))  # NEW — certificate file path

    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship('InventoryItem', backref='supplier', lazy=True)

    # NEW relationships
    payments = db.relationship('SupplierPayment', backref='supplier', lazy=True,
                               cascade='all, delete-orphan',
                               order_by='SupplierPayment.payment_date.desc()')
    warranties = db.relationship('SupplierWarranty', backref='supplier', lazy=True,
                                 cascade='all, delete-orphan')
    amcs = db.relationship('SupplierAMC', backref='supplier', lazy=True,
                           cascade='all, delete-orphan')


# ========== SUPPLIER PAYMENT ==========
class SupplierPayment(db.Model):
    """
    One record per payment made to a supplier.
    Supports Card, UPI, Cash, Bank Transfer, Cheque.
    Receipt file (screenshot / PDF / JPEG) stored in uploads/supplier_payments/.
    """
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)

    payment_date = db.Column(db.DateTime, default=datetime.utcnow)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default='INR')

    # Payment method: cash | card | upi | bank_transfer | cheque | other
    payment_method = db.Column(db.String(50), nullable=False, default='cash')

    # Method-specific references
    upi_id = db.Column(db.String(100))          # UPI VPA used
    utr_reference = db.Column(db.String(100))   # UPI / NEFT / RTGS UTR
    card_last4 = db.Column(db.String(4))        # Last 4 digits of card
    card_type = db.Column(db.String(20))        # Visa / Mastercard / Rupay / Amex
    bank_name = db.Column(db.String(100))       # Bank for transfer or cheque
    cheque_number = db.Column(db.String(50))    # Cheque no.
    account_number = db.Column(db.String(50))   # Beneficiary account (masked)
    ifsc_code = db.Column(db.String(20))        # IFSC for bank transfer

    purpose = db.Column(db.String(200))         # What this payment was for
    invoice_ref = db.Column(db.String(100))     # Supplier invoice / bill number
    notes = db.Column(db.Text)

    # Receipt attachment: screenshot / PDF / JPEG
    receipt_file = db.Column(db.String(500))            # stored filename
    receipt_original_name = db.Column(db.String(200))   # original upload name

    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ========== SUPPLIER WARRANTY / INSURANCE ==========
class SupplierWarranty(db.Model):
    """
    Warranty or Insurance policy details for a supplier/vendor.
    Covers: product warranty, extended warranty, insurance policy.
    """
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)

    # Type: product_warranty | extended_warranty | insurance
    warranty_type = db.Column(db.String(50), default='product_warranty')

    # Issuing company details
    issuer_company = db.Column(db.String(200))      # Warranty / Insurance company name
    issuer_address = db.Column(db.Text)
    issuer_phone = db.Column(db.String(20))
    issuer_email = db.Column(db.String(120))
    issuer_website = db.Column(db.String(200))

    # Policy / Certificate details
    policy_number = db.Column(db.String(100))
    certificate_number = db.Column(db.String(100))
    start_date = db.Column(db.DateTime)
    expiry_date = db.Column(db.DateTime)
    coverage_amount = db.Column(db.Float)           # Insured / covered value
    premium_amount = db.Column(db.Float)            # Premium paid (for insurance)
    deductible = db.Column(db.Float)                # Excess / deductible
    coverage_details = db.Column(db.Text)           # What is covered

    notes = db.Column(db.Text)

    # Certificate / policy document
    document_file = db.Column(db.String(500))
    document_original_name = db.Column(db.String(200))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ========== SUPPLIER AMC ==========
class SupplierAMC(db.Model):
    """
    Annual Maintenance Contract (AMC) linked to a supplier.
    Tracks contract details, service schedule, and contract document.
    """
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'), nullable=False)

    # AMC provider details (may differ from the supplier itself)
    amc_company = db.Column(db.String(200))
    amc_address = db.Column(db.Text)
    amc_phone = db.Column(db.String(20))
    amc_email = db.Column(db.String(120))
    amc_contact_person = db.Column(db.String(100))

    # Contract details
    contract_number = db.Column(db.String(100))
    contract_type = db.Column(db.String(50), default='comprehensive')
    # comprehensive | labour_only | parts_only | preventive_only
    start_date = db.Column(db.DateTime)
    end_date = db.Column(db.DateTime)
    contract_value = db.Column(db.Float)

    # Service schedule
    first_service_due = db.Column(db.DateTime)     # NEW — first service date
    service_interval_months = db.Column(db.Integer, default=3)
    # Number of visits included per year
    visits_per_year = db.Column(db.Integer)

    # Response SLA
    response_time_hours = db.Column(db.Integer)    # e.g. 4, 8, 24
    escalation_contact = db.Column(db.String(200)) # Who to call if no response

    notes = db.Column(db.Text)

    # Contract document
    document_file = db.Column(db.String(500))
    document_original_name = db.Column(db.String(200))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ========== FUEL LOG (vehicles only) ==========
class FuelLog(db.Model):
    """
    Fuel / gas filling log for 2-wheeler and 4-wheeler assets.
    Shown conditionally on view_asset.html when category is a vehicle type.
    """
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'), nullable=False)

    fill_date = db.Column(db.DateTime, default=datetime.utcnow)

    # Fuel type: petrol | diesel | cng | electric | lpg
    fuel_type = db.Column(db.String(30), default='petrol')

    quantity_litres = db.Column(db.Float)           # Litres filled (None for electric/CNG by kg)
    quantity_unit = db.Column(db.String(10), default='litres')  # litres | kg | kWh
    cost_per_unit = db.Column(db.Float)             # ₹ per litre / kg / kWh
    total_cost = db.Column(db.Float)                # Total amount paid

    odometer_km = db.Column(db.Integer)             # Odometer reading at fill
    mileage_kmpl = db.Column(db.Float)              # Calculated or entered mileage

    station_name = db.Column(db.String(200))        # Petrol pump / charging station
    station_location = db.Column(db.String(200))
    filled_by = db.Column(db.String(100))           # Person who filled / logged

    # Full tank or partial
    is_full_tank = db.Column(db.Boolean, default=True)

    receipt_photo = db.Column(db.String(500))       # Receipt / meter photo
    receipt_original_name = db.Column(db.String(200))

    notes = db.Column(db.Text)

    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ========== INVENTORY ITEM ==========
class InventoryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # Basic info
    name = db.Column(db.String(200), nullable=False)
    asset_code = db.Column(db.String(100), unique=True, nullable=False)
    serial_number = db.Column(db.String(100), unique=True)

    # Categorisation
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    sub_category = db.Column(db.String(100))
    category = db.relationship('Category', backref='items')

    # Vendor
    supplier_id = db.Column(db.Integer, db.ForeignKey('supplier.id'))
    

    # Location & quantity
    location = db.Column(db.String(200))
    room = db.Column(db.String(100))
    floor = db.Column(db.String(20))

    # Financial
    purchase_cost = db.Column(db.Float)
    current_value = db.Column(db.Float)
    depreciation_rate = db.Column(db.Float, default=10)

    # Asset details
    brand = db.Column(db.String(100))
    model = db.Column(db.String(100))
    item_type = db.Column(db.String(20))        
    condition = db.Column(db.String(50))         
    age_months = db.Column(db.Integer)           

    # Dates
    purchase_date = db.Column(db.DateTime)
    warranty_expiry = db.Column(db.DateTime)
    warranty_registration_no = db.Column(db.String(100))

    insurance_expiry = db.Column(db.DateTime)
    insurance_provider = db.Column(db.String(200))
    insurance_policy_no = db.Column(db.String(100))
    insurance_amount = db.Column(db.Float)

    # Installation details
    installation_date = db.Column(db.DateTime)
    installation_by = db.Column(db.String(100))
    installation_ref_no = db.Column(db.String(100))
    installation_notes = db.Column(db.Text)
    installation_certificate = db.Column(db.String(500))
    warranty_card_no = db.Column(db.String(100)) 

    # AMC tracking
    amc_provider = db.Column(db.String(200))
    amc_start_date = db.Column(db.DateTime)
    amc_end_date = db.Column(db.DateTime)
    amc_cost = db.Column(db.Float)

    # Service tracking
    last_service_date = db.Column(db.DateTime)
    service_interval_days = db.Column(db.Integer, default=180)
    next_service_due = db.Column(db.DateTime)
    service_provider = db.Column(db.String(200))
    service_contact = db.Column(db.String(20))
    service_notes = db.Column(db.Text)

    # Media
    photo = db.Column(db.String(500))
    bill_copy = db.Column(db.String(500))

    # Status: active, retired, under_maintenance
    status = db.Column(db.String(50), default='active')

    # Multi-company
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    documents = db.relationship('AssetDocument', backref='item', lazy=True,
                                cascade='all, delete-orphan')
    maintenance_schedules = db.relationship('MaintenanceSchedule', backref='item', lazy=True,
                                            cascade='all, delete-orphan')
    service_logs = db.relationship('ServiceLog', backref='item', lazy=True,
                                   cascade='all, delete-orphan')
    alerts = db.relationship('Alert', backref='item', lazy=True,
                             cascade='all, delete-orphan')
    checklist_items = db.relationship('MaintenanceChecklist', backref='item', lazy=True,
                                      cascade='all, delete-orphan')

    # NEW — fuel logs (only used for vehicle categories)
    fuel_logs = db.relationship('FuelLog', backref='item', lazy=True,
                                cascade='all, delete-orphan',
                                order_by='FuelLog.fill_date.desc()')

    @property
    def is_vehicle(self):
        """Convenience shortcut used by view_asset.html to show the Fuel tab."""
        return self.category and self.category.is_vehicle


# ========== ASSET DOCUMENTS ==========
class AssetDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'), nullable=False)
    document_type = db.Column(db.String(50))
    file_path = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(200))
    file_size = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    notes = db.Column(db.Text)
    expiry_date = db.Column(db.DateTime)


# ========== MAINTENANCE TEMPLATE ==========
class MaintenanceTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    checklist_items = db.relationship('MaintenanceChecklistTemplate', backref='template',
                                      lazy=True, cascade='all, delete-orphan',
                                      order_by='MaintenanceChecklistTemplate.sort_order')


class MaintenanceChecklistTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('maintenance_template.id'), nullable=False)
    task_name = db.Column(db.String(200), nullable=False)
    instructions = db.Column(db.Text)
    is_critical = db.Column(db.Boolean, default=False)
    sort_order = db.Column(db.Integer, default=0)


# ========== MAINTENANCE CHECKLIST (per-schedule instance) ==========
class MaintenanceChecklist(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'), nullable=False)
    maintenance_schedule_id = db.Column(db.Integer, db.ForeignKey('maintenance_schedule.id'))
    task_name = db.Column(db.String(200), nullable=False)
    is_completed = db.Column(db.Boolean, default=False)
    completed_at = db.Column(db.DateTime)
    completed_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    remarks = db.Column(db.Text)
    photo_evidence = db.Column(db.String(500))


# ========== MAINTENANCE SCHEDULE ==========
class MaintenanceSchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'), nullable=False)
    due_date = db.Column(db.DateTime, nullable=False)
    frequency_days = db.Column(db.Integer)
    last_performed = db.Column(db.DateTime)
    status = db.Column(db.String(50), default='pending')
    priority = db.Column(db.String(20), default='normal')
    escalation_level = db.Column(db.Integer, default=0)
    assigned_to = db.Column(db.Integer, db.ForeignKey('user.id'))
    notes = db.Column(db.Text)
    total_cost = db.Column(db.Float, default=0)

    checklist_items = db.relationship('MaintenanceChecklist', backref='schedule', lazy=True,
                                      foreign_keys='MaintenanceChecklist.maintenance_schedule_id')


# ========== SERVICE LOG ==========
class ServiceLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'), nullable=False)
    maintenance_schedule_id = db.Column(db.Integer, db.ForeignKey('maintenance_schedule.id'))
    service_date = db.Column(db.DateTime, default=datetime.utcnow)
    performed_by = db.Column(db.String(100))
    actions = db.Column(db.Text)
    parts_used = db.Column(db.Text)
    cost = db.Column(db.Float)
    next_service_due = db.Column(db.DateTime)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ========== ALERT SETTINGS ==========
class AlertSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    alert_type = db.Column(db.String(50))
    days_before = db.Column(db.Integer, default=30)
    escalation_days = db.Column(db.Integer, default=7)
    send_email = db.Column(db.Boolean, default=True)
    send_whatsapp = db.Column(db.Boolean, default=False)
    send_sms = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)


# ========== ALERTS ==========
class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'))
    alert_type = db.Column(db.String(50))
    message = db.Column(db.Text)
    triggered_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)
    is_escalated = db.Column(db.Boolean, default=False)
    escalation_level = db.Column(db.Integer, default=0)
    resolved_at = db.Column(db.DateTime)
    resolved_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    notification_sent = db.Column(db.Boolean, default=False)
    notification_method = db.Column(db.String(50))


# ========== ACTIVITY LOG ==========
class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    action = db.Column(db.String(200))
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(50))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
