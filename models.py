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
            'Electronics', 'Furniture', 'HVAC', 'Kitchen Appliances',
            'Vehicle', 'IT Equipment', 'Medical Equipment', 'Safety Equipment',
            'Office Equipment', 'Tools', 'Machinery', 'Lighting', 'Plumbing'
        ]


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
    payment_terms = db.Column(db.String(200))
    rating = db.Column(db.Integer, default=3)  # 1–5
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship('InventoryItem', backref='supplier', lazy=True)


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
    quantity = db.Column(db.Integer, default=1)

    # Financial
    purchase_cost = db.Column(db.Float)
    current_value = db.Column(db.Float)
    depreciation_rate = db.Column(db.Float, default=10)  # % per year

    # Asset details
    brand = db.Column(db.String(100))
    model = db.Column(db.String(100))
    condition = db.Column(db.String(50))

    # Dates
    purchase_date = db.Column(db.DateTime)
    warranty_expiry = db.Column(db.DateTime)
    insurance_expiry = db.Column(db.DateTime)

    # AMC tracking
    amc_provider = db.Column(db.String(200))
    amc_start_date = db.Column(db.DateTime)
    amc_end_date = db.Column(db.DateTime)
    amc_cost = db.Column(db.Float)

    # Stock alerts
    reorder_level = db.Column(db.Integer, default=0)

    # Media
    photo = db.Column(db.String(500))

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


# ========== ASSET DOCUMENTS ==========
class AssetDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('inventory_item.id'), nullable=False)
    document_type = db.Column(db.String(50))  # invoice, warranty, manual, insurance, service_bill
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
    status = db.Column(db.String(50), default='pending')  # pending, completed, overdue, escalated
    priority = db.Column(db.String(20), default='normal')  # low, normal, high, critical
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
    # Types: warranty, maintenance, insurance, low_stock, amc, high_value
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
    notification_method = db.Column(db.String(50))  # email, whatsapp, sms


# ========== ACTIVITY LOG ==========
class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'))
    action = db.Column(db.String(200))
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(50))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
