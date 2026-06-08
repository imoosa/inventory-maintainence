from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from flask_mail import Mail, Message
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
from functools import wraps
import os
import json
import qrcode
from io import BytesIO
import base64

# Load environment variables
load_dotenv()

# Initialize app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-key-change-this')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///inventory.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# File upload config
app.config['UPLOAD_FOLDER_PHOTOS'] = 'static/uploads/photos'
app.config['UPLOAD_FOLDER_DOCS'] = 'static/uploads/documents'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  
app.config['UPLOAD_FOLDER_SUPPLIER_PAYMENTS'] = 'static/uploads/supplier_payments'
app.config['UPLOAD_FOLDER_SUPPLIER_DOCS'] = 'static/uploads/supplier_docs'
app.config['UPLOAD_FOLDER_FUEL_RECEIPTS'] = 'static/uploads/fuel_receipts'

# Email config
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'true').lower() == 'true'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_USERNAME')

# WhatsApp / Twilio config
app.config['TWILIO_ACCOUNT_SID'] = os.getenv('TWILIO_ACCOUNT_SID')
app.config['TWILIO_AUTH_TOKEN'] = os.getenv('TWILIO_AUTH_TOKEN')
app.config['TWILIO_WHATSAPP_FROM'] = os.getenv('TWILIO_WHATSAPP_FROM', 'whatsapp:+14155238886')
app.config['TWILIO_SMS_FROM'] = os.getenv('TWILIO_SMS_FROM')

# High-value item alert threshold (configurable via .env)
app.config['HIGH_VALUE_THRESHOLD'] = float(os.getenv('HIGH_VALUE_THRESHOLD', 50000))

# Initialize extensions
from models import (db, User, Company, Category, Supplier, InventoryItem,
                    AssetDocument, MaintenanceSchedule, MaintenanceChecklist,
                    MaintenanceTemplate, MaintenanceChecklistTemplate,
                    ServiceLog, Alert, AlertSetting, ActivityLog,
                    # NEW ↓
                    SupplierPayment, SupplierWarranty, SupplierAMC, FuelLog)
db.init_app(app)
mail = Mail(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'

# Create upload directories
os.makedirs(app.config['UPLOAD_FOLDER_PHOTOS'], exist_ok=True)
os.makedirs(app.config['UPLOAD_FOLDER_DOCS'], exist_ok=True)
os.makedirs('static/reports', exist_ok=True)

 
# ADD THESE to the os.makedirs block:
os.makedirs(app.config['UPLOAD_FOLDER_SUPPLIER_PAYMENTS'], exist_ok=True)
os.makedirs(app.config['UPLOAD_FOLDER_SUPPLIER_DOCS'], exist_ok=True)
os.makedirs(app.config['UPLOAD_FOLDER_FUEL_RECEIPTS'], exist_ok=True)


# Import utilities
from utils.alerts import AlertManager
from utils.reports import ReportGenerator

alert_manager = AlertManager(app, mail)
report_gen = ReportGenerator()

# ========== SCHEDULED JOBS ==========
scheduler = BackgroundScheduler()

def scheduled_alert_check():
    """Run every hour to check alerts and escalate"""
    with app.app_context():
        check_all_alerts()
        escalate_overdue_alerts()

scheduler.add_job(func=scheduled_alert_check, trigger="interval", hours=1)
scheduler.start()


# ========== ROLE-BASED ACCESS DECORATORS ==========

def role_required(*roles):
    """Decorator to restrict routes to specific roles."""
    def decorator(f):
        @wraps(f)
        @login_required
        def decorated_function(*args, **kwargs):
            if current_user.role not in roles:
                flash('You do not have permission to access this page.', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def admin_required(f):
    return role_required('admin')(f)

def manager_or_admin(f):
    return role_required('admin', 'manager')(f)


# ========== HELPER FUNCTIONS ==========

def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx', 'xls', 'xlsx'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def log_activity(user_id, action, details):
    try:
        log = ActivityLog(
            user_id=user_id,
            company_id=current_user.company_id if hasattr(current_user, 'company_id') else None,
            action=action,
            details=details,
            ip_address=request.remote_addr
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        print(f"Activity log error: {e}")


def validate_item_form(form):
    """
    Server-side validation for inventory item form.
    Returns (is_valid: bool, errors: list[str])
    """
    errors = []
    if not form.get('name', '').strip():
        errors.append('Item name is required.')
    if not form.get('category_id'):
        errors.append('Category is required.')
    qty = form.get('quantity', '')
    if not qty or not qty.isdigit() or int(qty) < 0:
        errors.append('Quantity must be a non-negative whole number.')
    cost = form.get('purchase_cost', '')
    try:
        if cost:
            float(cost)
    except ValueError:
        errors.append('Purchase cost must be a valid number.')
    # Validate date fields
    for field in ['purchase_date', 'warranty_expiry', 'insurance_expiry',
                  'amc_start_date', 'amc_end_date']:
        val = form.get(field, '')
        if val:
            try:
                datetime.strptime(val, '%Y-%m-%d')
            except ValueError:
                errors.append(f'{field.replace("_", " ").title()} must be a valid date (YYYY-MM-DD).')
    return len(errors) == 0, errors


def check_all_alerts():
    """Check all alert conditions per item and create alerts where needed."""
    items = InventoryItem.query.all()
    settings = AlertSetting.query.all()
    settings_dict = {s.alert_type: s for s in settings}
    today = datetime.utcnow().date()

    for item in items:
        # --- Warranty expiry ---
        if item.warranty_expiry:
            days_left = (item.warranty_expiry.date() - today).days
            setting = settings_dict.get('warranty', AlertSetting(days_before=30))
            if setting.is_active and 0 <= days_left <= setting.days_before:
                _create_alert(item, 'warranty', f"Warranty expires in {days_left} day(s)")

        # --- Maintenance overdue ---
        maintenance = MaintenanceSchedule.query.filter_by(
            item_id=item.id, status='pending'
        ).first()
        if maintenance and maintenance.due_date.date() <= today:
            days_overdue = (today - maintenance.due_date.date()).days
            setting = settings_dict.get('maintenance', AlertSetting(days_before=0))
            if setting.is_active:
                _create_alert(item, 'maintenance',
                              f"Maintenance overdue by {days_overdue} day(s)")

        # --- Insurance expiry ---
        if item.insurance_expiry:
            days_left = (item.insurance_expiry.date() - today).days
            setting = settings_dict.get('insurance', AlertSetting(days_before=30))
            if setting.is_active and 0 <= days_left <= setting.days_before:
                _create_alert(item, 'insurance', f"Insurance expires in {days_left} day(s)")

        # --- AMC expiry (NEW) ---
        if item.amc_end_date:
            days_left = (item.amc_end_date.date() - today).days
            setting = settings_dict.get('amc', AlertSetting(days_before=30))
            if setting.is_active and 0 <= days_left <= setting.days_before:
                _create_alert(item, 'amc', f"AMC expires in {days_left} day(s) for {item.name}")

        # --- Low stock ---
        if item.reorder_level and item.quantity <= item.reorder_level:
            setting = settings_dict.get('low_stock', AlertSetting(days_before=0))
            if setting.is_active:
                _create_alert(item, 'low_stock',
                              f"Low stock: only {item.quantity} unit(s) left (reorder at {item.reorder_level})")

        # --- High value item check (NEW) ---
        threshold = app.config['HIGH_VALUE_THRESHOLD']
        if item.purchase_cost and item.purchase_cost >= threshold:
            setting = settings_dict.get('high_value', AlertSetting(days_before=0))
            if setting.is_active:
                _create_alert(item, 'high_value',
                              f"High-value asset: {item.name} valued at ₹{item.purchase_cost:,.0f}")


def _create_alert(item, alert_type, message):
    """Create a deduplicated alert and dispatch notifications."""
    existing = Alert.query.filter_by(
        item_id=item.id, alert_type=alert_type, resolved_at=None
    ).first()
    if existing:
        return  # already open — do not duplicate

    alert = Alert(item_id=item.id, alert_type=alert_type, message=message)
    db.session.add(alert)
    db.session.commit()

    # Dispatch notifications based on per-type settings
    setting = AlertSetting.query.filter_by(
        company_id=item.company_id, alert_type=alert_type
    ).first()

    users = User.query.filter_by(company_id=item.company_id).all()
    for user in users:
        if setting:
            if setting.send_email:
                _send_email_alert(alert, user, item)
            if setting.send_whatsapp and user.phone:
                _send_whatsapp_alert(alert, user, item)
            if setting.send_sms and user.phone:
                _send_sms_alert(alert, user, item)
        else:
            # Default: email only
            _send_email_alert(alert, user, item)

    alert.notification_sent = True
    db.session.commit()


def _send_email_alert(alert, user, item):
    """Send email notification for an alert."""
    try:
        subject = f"[Inventory Alert] {alert.alert_type.replace('_', ' ').title()} — {item.name}"
        body = (
            f"Dear {user.username},\n\n"
            f"Alert: {alert.message}\n\n"
            f"Asset: {item.name} (Code: {item.asset_code})\n"
            f"Location: {item.location or 'N/A'}\n\n"
            f"Please log in to review and resolve this alert.\n"
        )
        msg = Message(subject=subject, recipients=[user.email], body=body)
        mail.send(msg)
        alert.notification_method = 'email'
    except Exception as e:
        print(f"Email alert error: {e}")


def _send_whatsapp_alert(alert, user, item):
    """Send WhatsApp notification via Twilio."""
    try:
        from twilio.rest import Client
        sid = app.config.get('TWILIO_ACCOUNT_SID')
        token = app.config.get('TWILIO_AUTH_TOKEN')
        from_number = app.config.get('TWILIO_WHATSAPP_FROM')
        if not all([sid, token, from_number]):
            print("WhatsApp: Twilio credentials not configured.")
            return
        client = Client(sid, token)
        body = (
            f"*Inventory Alert*\n"
            f"{alert.message}\n"
            f"Asset: {item.name} ({item.asset_code})\n"
            f"Location: {item.location or 'N/A'}"
        )
        client.messages.create(
            from_=from_number,
            to=f"whatsapp:{user.phone}",
            body=body
        )
        alert.notification_method = 'whatsapp'
    except ImportError:
        print("Twilio library not installed. Run: pip install twilio")
    except Exception as e:
        print(f"WhatsApp alert error: {e}")


def _send_sms_alert(alert, user, item):
    """Send SMS notification via Twilio."""
    try:
        from twilio.rest import Client
        sid = app.config.get('TWILIO_ACCOUNT_SID')
        token = app.config.get('TWILIO_AUTH_TOKEN')
        from_number = app.config.get('TWILIO_SMS_FROM')
        if not all([sid, token, from_number]):
            print("SMS: Twilio credentials not configured.")
            return
        client = Client(sid, token)
        body = f"Inventory Alert: {alert.message} | Asset: {item.name}"
        client.messages.create(
            from_=from_number,
            to=user.phone,
            body=body
        )
        alert.notification_method = 'sms'
    except ImportError:
        print("Twilio library not installed. Run: pip install twilio")
    except Exception as e:
        print(f"SMS alert error: {e}")


def escalate_overdue_alerts():
    """Escalate alerts that have been open beyond the escalation threshold."""
    settings = {s.alert_type: s for s in AlertSetting.query.all()}
    open_alerts = Alert.query.filter_by(resolved_at=None, is_escalated=False).all()

    for alert in open_alerts:
        setting = settings.get(alert.alert_type)
        if not setting or not setting.escalation_days:
            continue
        age_days = (datetime.utcnow() - alert.triggered_at).days
        if age_days >= setting.escalation_days:
            alert.is_escalated = True
            alert.escalation_level = min(alert.escalation_level + 1, 2)
            # Notify managers/admins
            if alert.item and alert.item.company_id:
                managers = User.query.filter(
                    User.company_id == alert.item.company_id,
                    User.role.in_(['admin', 'manager'])
                ).all()
                for mgr in managers:
                    try:
                        msg = Message(
                            subject=f"[ESCALATED] {alert.alert_type.title()} alert unresolved for {age_days} days",
                            recipients=[mgr.email],
                            body=(
                                f"Dear {mgr.username},\n\n"
                                f"The following alert has not been resolved in {age_days} days:\n\n"
                                f"{alert.message}\n\n"
                                f"Please take immediate action."
                            )
                        )
                        mail.send(msg)
                    except Exception as e:
                        print(f"Escalation email error: {e}")
    db.session.commit()


def calculate_depreciation(item):
    """Calculate current value based on straight-line depreciation."""
    if not item.purchase_date or not item.purchase_cost:
        return item.purchase_cost
    years_old = (datetime.utcnow() - item.purchase_date).days / 365.25
    rate = item.depreciation_rate or 10
    current_value = item.purchase_cost * ((100 - rate) / 100) ** years_old
    return max(0, current_value)


def seed_checklist_from_template(schedule, item):
    """Seed MaintenanceChecklist rows from the template matching this item's category."""
    if not item.category_id:
        return
    template = MaintenanceTemplate.query.filter_by(
        category_id=item.category_id
    ).first()
    if not template:
        return
    for tpl_item in template.checklist_items:
        checklist_row = MaintenanceChecklist(
            item_id=item.id,
            maintenance_schedule_id=schedule.id,
            task_name=tpl_item.task_name,
        )
        db.session.add(checklist_row)


def _create_initial_data():
    """One-time seed — only runs if DB is empty."""
    if Company.query.first():
        return

    default_company = Company(name="Default Company", email="admin@example.com")
    db.session.add(default_company)
    db.session.commit()

    admin_username = os.getenv('ADMIN_USERNAME', 'admin')
    admin_password = os.getenv('ADMIN_PASSWORD', 'ChangeMe123!')
    admin_email = os.getenv('ADMIN_EMAIL', 'admin@example.com')

    admin = User(
        username=admin_username,
        email=admin_email,
        role='admin',
        company_id=default_company.id,
        is_active=True
    )
    admin.set_password(admin_password)
    db.session.add(admin)

    for cat_name in Category.get_predefined_categories():
        db.session.add(Category(name=cat_name, company_id=default_company.id))

    for alert_type in ['warranty', 'maintenance', 'insurance', 'low_stock', 'amc', 'high_value']:
        db.session.add(AlertSetting(
            company_id=default_company.id,
            alert_type=alert_type,
            days_before=30,
            escalation_days=7,
            send_email=True,
            is_active=True
        ))

    db.session.commit()
    print(f"✓ Admin created: {admin_username} / {admin_password}")


# ========== LOGIN MANAGER ==========

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ========== AUTH ROUTES ==========

@app.route('/')
def index():
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.is_active and user.check_password(password):
            login_user(user)
            log_activity(user.id, 'login', f'Logged in from {request.remote_addr}')
            flash(f'Welcome back, {user.username}!', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    log_activity(current_user.id, 'logout', 'User logged out')
    logout_user()
    return redirect(url_for('login'))


# ========== DASHBOARD ==========

@app.route('/dashboard')
@login_required
def dashboard():
    try:
        company_id = current_user.company_id
        from sqlalchemy import func
        from datetime import timezone, datetime as dt

        total_items = InventoryItem.query.filter_by(company_id=company_id).count()
        
        # Count pending maintenance
        pending_maintenance = MaintenanceSchedule.query.join(InventoryItem).filter(
            InventoryItem.company_id == company_id,
            MaintenanceSchedule.status == 'pending'
        ).count()
        
        # Total maintenance cost
        total_maintenance_cost = db.session.query(
            func.sum(ServiceLog.cost)
        ).join(InventoryItem).filter(
            InventoryItem.company_id == company_id
        ).scalar() or 0

        # Use timezone-aware datetime for Python 3.13+ compatibility
        now = dt.now(timezone.utc)
        
        # Monthly maintenance cost (last 6 months)
        monthly_costs = []
        for i in range(5, -1, -1):
            ref = now.replace(day=1) - timedelta(days=30 * i)
            month_start = ref.replace(day=1)
            month_end = (month_start + timedelta(days=32)).replace(day=1)
            cost = db.session.query(func.sum(ServiceLog.cost)).join(InventoryItem).filter(
                InventoryItem.company_id == company_id,
                ServiceLog.service_date >= month_start,
                ServiceLog.service_date < month_end
            ).scalar() or 0
            monthly_costs.append({'month': month_start.strftime('%b %Y'), 'cost': float(cost)})

        # Category-wise value
        category_values = db.session.query(
            Category.name, func.sum(InventoryItem.current_value)
        ).join(InventoryItem, isouter=True).filter(
            InventoryItem.company_id == company_id
        ).group_by(Category.id).all()

        # Recent alerts
        recent_alerts = Alert.query.join(InventoryItem).filter(
            InventoryItem.company_id == company_id
        ).order_by(Alert.triggered_at.desc()).limit(10).all()

        # Upcoming maintenance (next 30 days)
        upcoming_maintenance = MaintenanceSchedule.query.join(InventoryItem).filter(
            InventoryItem.company_id == company_id,
            MaintenanceSchedule.status == 'pending',
            MaintenanceSchedule.due_date <= now + timedelta(days=30)
        ).order_by(MaintenanceSchedule.due_date).limit(5).all()

        # Pass now to template for date calculations
        return render_template('dashboard.html',
                               total_items=total_items,
                               low_stock_items=0,  # Not used in home inventory
                               pending_maintenance=pending_maintenance,
                               total_maintenance_cost=total_maintenance_cost,
                               monthly_costs=monthly_costs,
                               category_values=category_values,
                               recent_alerts=recent_alerts,
                               upcoming_maintenance=upcoming_maintenance,
                               now=now)  # Pass current time to template
    except Exception as e:
        print(f"Dashboard error: {e}")
        flash(f'Error loading dashboard: {e}', 'danger')
        from datetime import timezone, datetime as dt
        now = dt.now(timezone.utc)
        return render_template('dashboard.html',
                               total_items=0, low_stock_items=0, pending_maintenance=0,
                               total_maintenance_cost=0, monthly_costs=[],
                               category_values=[], recent_alerts=[], upcoming_maintenance=[],
                               now=now)


# ========== INVENTORY ROUTES ==========

@app.route('/inventory')
@login_required
def inventory():
    # Filters
    category_id = request.args.get('category_id', type=int)
    status = request.args.get('status')
    search = request.args.get('search', '').strip()

    query = InventoryItem.query.filter_by(company_id=current_user.company_id)
    if category_id:
        query = query.filter_by(category_id=category_id)
    if status:
        query = query.filter_by(status=status)
    if search:
        query = query.filter(InventoryItem.name.ilike(f'%{search}%'))

    items = query.order_by(InventoryItem.name).all()
    categories = Category.query.filter_by(company_id=current_user.company_id).all()
    return render_template('inventory.html', items=items, categories=categories,
                           selected_category=category_id, selected_status=status, search=search)


@app.route('/inventory/add', methods=['GET', 'POST'])
@login_required
def add_item():
    categories = Category.query.filter_by(company_id=current_user.company_id).all()
    suppliers = Supplier.query.filter_by(company_id=current_user.company_id).all()

    if request.method == 'POST':
        # Simplified validation
        if not request.form.get('name', '').strip():
            flash('Item name is required.', 'danger')
            return render_template('add_item.html', categories=categories, suppliers=suppliers)

        # Generate asset code
        asset_code = request.form.get('asset_code', '').strip()
        if not asset_code:
            asset_code = f"HOME-{datetime.now().strftime('%Y%m%d%H%M%S')}"

        # Photo upload
        photo_filename = None
        if 'photo' in request.files:
            file = request.files['photo']
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                photo_filename = f"{datetime.utcnow().timestamp()}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER_PHOTOS'], photo_filename))

        # Bill upload
        bill_filename = None
        if 'bill_copy' in request.files:
            file = request.files['bill_copy']
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                bill_filename = f"{datetime.utcnow().timestamp()}_bill_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER_DOCS'], bill_filename))

        def parse_date(field):
            val = request.form.get(field, '').strip()
            return datetime.strptime(val, '%Y-%m-%d') if val else None

        # Calculate next service due
        last_service = parse_date('last_service_date')
        service_interval = int(request.form.get('service_interval_days', 0))
        next_service = None
        if last_service and service_interval > 0:
            next_service = last_service + timedelta(days=service_interval)

        item = InventoryItem(
            asset_code=asset_code,
            name=request.form['name'].strip(),
            supplier_id=int(request.form['supplier_id']) if request.form.get('supplier_id') else None,
            category_id=int(request.form['category_id']) if request.form.get('category_id') else None,
            brand=request.form.get('brand', '').strip() or None,
            model=request.form.get('model', '').strip() or None,
            serial_number=request.form.get('serial_number', '').strip() or None,
            location=request.form.get('location', '').strip() or None,
            room=request.form.get('room', '').strip() or None,
            purchase_cost=float(request.form.get('purchase_cost', 0)) if request.form.get('purchase_cost') else None,
            # REMOVED: purchase_place=request.form.get('purchase_place', '').strip() or None,
            purchase_date=parse_date('purchase_date'),
            warranty_expiry=parse_date('warranty_expiry'),
            warranty_registration_no=request.form.get('warranty_registration_no', '').strip() or None,
            insurance_provider = request.form.get('insurance_provider', '').strip() or None,
            insurance_expiry = parse_date('insurance_expiry'),
            insurance_policy_no = request.form.get('insurance_policy_no', '').strip() or None,
            insurance_amount = float(request.form.get('insurance_amount', 0)) if request.form.get('insurance_amount') else None,
            amc_provider=request.form.get('amc_provider', '').strip() or None,
            amc_start_date=parse_date('amc_start_date'),
            amc_end_date=parse_date('amc_end_date'),
            amc_cost=float(request.form.get('amc_cost', 0)) if request.form.get('amc_cost') else None,
            last_service_date=parse_date('last_service_date'),
            service_interval_days=service_interval,
            next_service_due=next_service,
            service_provider=request.form.get('service_provider', '').strip() or None,
            service_contact=request.form.get('service_contact', '').strip() or None,
            condition=request.form.get('condition', 'Good'),
            photo=photo_filename,
            bill_copy=bill_filename,
            status=request.form.get('status', 'active'),
            company_id=current_user.company_id
        )
        
        db.session.add(item)
        db.session.commit()

        # Create maintenance schedule if service interval set
        if next_service:
            schedule = MaintenanceSchedule(
                item_id=item.id,
                due_date=next_service,
                frequency_days=service_interval,
                status='pending',
                notes=f"Regular service for {item.name}"
            )
            db.session.add(schedule)
            db.session.commit()

        flash(f'✅ {item.name} added successfully!', 'success')
        return redirect(url_for('inventory'))

    return render_template('add_item.html', categories=categories, suppliers=suppliers)


@app.route('/inventory/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_item(id):
    item = InventoryItem.query.get_or_404(id)
    if item.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('inventory'))

    categories = Category.query.filter_by(company_id=current_user.company_id).all()
    suppliers = Supplier.query.filter_by(company_id=current_user.company_id).all()

    if request.method == 'POST':
        def parse_date(field):
            val = request.form.get(field, '').strip()
            return datetime.strptime(val, '%Y-%m-%d') if val else None

        # Update basic fields
        item.name = request.form['name'].strip()
        item.category_id = int(request.form['category_id']) if request.form.get('category_id') else None
        item.supplier_id = int(request.form['supplier_id']) if request.form.get('supplier_id') else None
        item.brand = request.form.get('brand', '').strip() or None
        item.model = request.form.get('model', '').strip() or None
        item.serial_number = request.form.get('serial_number', '').strip() or None
        item.location = request.form.get('location', '').strip() or None
        item.room = request.form.get('room', '').strip() or None
        item.purchase_cost = float(request.form.get('purchase_cost', 0)) if request.form.get('purchase_cost') else None
        item.purchase_date = parse_date('purchase_date')
        
        # Item type and condition
        item.item_type = request.form.get('item_type') or None
        if item.item_type == 'Second Hand':
            item.condition = request.form.get('condition') or None
            item.age_months = int(request.form.get('age_months')) if request.form.get('age_months') else None
        else:
            item.condition = None
            item.age_months = None
        
        # Warranty
        item.warranty_expiry = parse_date('warranty_expiry')
        item.warranty_registration_no = request.form.get('warranty_registration_no', '').strip() or None
        
        # Insurance
        item.insurance_provider = request.form.get('insurance_provider', '').strip() or None
        item.insurance_expiry = parse_date('insurance_expiry')
        item.insurance_policy_no = request.form.get('insurance_policy_no', '').strip() or None
        item.insurance_amount = float(request.form.get('insurance_amount', 0)) if request.form.get('insurance_amount') else None
        
        # AMC
        item.amc_provider = request.form.get('amc_provider', '').strip() or None
        item.amc_start_date = parse_date('amc_start_date')
        item.amc_end_date = parse_date('amc_end_date')
        item.amc_cost = float(request.form.get('amc_cost', 0)) if request.form.get('amc_cost') else None
        
        # Service
        item.last_service_date = parse_date('last_service_date')
        item.service_interval_days = int(request.form.get('service_interval_days', 180))
        item.service_provider = request.form.get('service_provider', '').strip() or None
        item.service_contact = request.form.get('service_contact', '').strip() or None
        
        # Calculate next service due
        if item.last_service_date and item.service_interval_days > 0:
            item.next_service_due = item.last_service_date + timedelta(days=item.service_interval_days)
        else:
            item.next_service_due = None
        
        # Status
        item.status = request.form.get('status', 'active')
        
        # Installation details
        item.installation_date = parse_date('installation_date')
        item.installation_by = request.form.get('installation_by', '').strip() or None
        item.installation_ref_no = request.form.get('installation_ref_no', '').strip() or None
        item.installation_notes = request.form.get('installation_notes', '').strip() or None
        item.warranty_card_no = request.form.get('warranty_card_no', '').strip() or None
        
        # Installation certificate upload
        if 'installation_certificate' in request.files:
            cert = request.files['installation_certificate']
            if cert and cert.filename and allowed_file(cert.filename):
                safe = secure_filename(cert.filename)
                cert_filename = f"{datetime.utcnow().timestamp()}_{safe}"
                cert.save(os.path.join(app.config['UPLOAD_FOLDER_DOCS'], cert_filename))
                item.installation_certificate = cert_filename
        
        # Photo update
        if 'photo' in request.files:
            file = request.files['photo']
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                photo_filename = f"{datetime.utcnow().timestamp()}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER_PHOTOS'], photo_filename))
                item.photo = photo_filename
        
        # Bill copy update
        if 'bill_copy' in request.files:
            file = request.files['bill_copy']
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                bill_filename = f"{datetime.utcnow().timestamp()}_bill_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER_DOCS'], bill_filename))
                item.bill_copy = bill_filename
        
        item.updated_at = datetime.utcnow()
        
        db.session.commit()
        
        # Update or create maintenance schedule based on service interval
        existing_schedule = MaintenanceSchedule.query.filter_by(item_id=item.id).first()
        if item.next_service_due:
            if existing_schedule:
                existing_schedule.due_date = item.next_service_due
                existing_schedule.frequency_days = item.service_interval_days
                existing_schedule.status = 'pending'
            else:
                schedule = MaintenanceSchedule(
                    item_id=item.id,
                    due_date=item.next_service_due,
                    frequency_days=item.service_interval_days,
                    status='pending',
                    notes=f"Regular service for {item.name}"
                )
                db.session.add(schedule)
        elif existing_schedule and not item.next_service_due:
            db.session.delete(existing_schedule)
        
        db.session.commit()
        
        log_activity(current_user.id, 'edit_item', f'Edited item: {item.name} ({item.asset_code})')
        flash('Item updated successfully!', 'success')
        return redirect(url_for('inventory'))

    # Pass current datetime for the template
    from datetime import datetime as dt
    return render_template('edit_item.html', item=item, categories=categories, 
                          suppliers=suppliers, now=dt.utcnow())


@app.route('/inventory/delete/<int:id>', methods=['POST'])
@login_required
@manager_or_admin
def delete_item(id):
    item = InventoryItem.query.get_or_404(id)
    if item.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('inventory'))
    log_activity(current_user.id, 'delete_item', f'Deleted item: {item.name}')
    db.session.delete(item)
    db.session.commit()
    flash('Item deleted successfully!', 'success')
    return redirect(url_for('inventory'))

@app.route('/asset/<int:item_id>')
@login_required
def view_asset(item_id):
    item = InventoryItem.query.get_or_404(item_id)
    if item.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('inventory'))
    return render_template('view_asset.html', item=item, now=datetime.utcnow())


# ========== DOCUMENT MANAGEMENT ==========

@app.route('/inventory/<int:id>/documents')
@login_required
def view_documents(id):
    item = InventoryItem.query.get_or_404(id)
    if item.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('inventory'))
    return render_template('documents.html', item=item)


@app.route('/documents/upload/<int:item_id>', methods=['POST'])
@login_required
def upload_document(item_id):
    item = InventoryItem.query.get_or_404(item_id)
    if item.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('inventory'))

    doc_type = request.form.get('document_type', 'other')
    if 'document' in request.files:
        file = request.files['document']
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            doc_filename = f"{datetime.utcnow().timestamp()}_{doc_type}_{filename}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER_DOCS'], doc_filename))
            document = AssetDocument(
                item_id=item.id,
                document_type=doc_type,
                file_path=doc_filename,
                original_filename=file.filename,
                uploaded_by=current_user.id,
                notes=request.form.get('notes')
            )
            if request.form.get('expiry_date'):
                document.expiry_date = datetime.strptime(request.form['expiry_date'], '%Y-%m-%d')
            db.session.add(document)
            db.session.commit()
            flash('Document uploaded successfully!', 'success')
        else:
            flash('Invalid file type.', 'danger')
    return redirect(url_for('view_documents', id=item.id))


# ========== CATEGORY MANAGEMENT ==========

@app.route('/categories')
@login_required
def categories():
    cats = Category.query.filter_by(company_id=current_user.company_id).all()
    return render_template('categories.html', categories=cats)


@app.route('/categories/add', methods=['POST'])
@login_required
@manager_or_admin
def add_category():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Category name is required.', 'danger')
        return redirect(url_for('categories'))
    category = Category(
        name=name,
        description=request.form.get('description'),
        icon=request.form.get('icon'),
        company_id=current_user.company_id
    )
    db.session.add(category)
    db.session.commit()
    flash('Category added!', 'success')
    return redirect(url_for('categories'))


@app.route('/categories/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@manager_or_admin
def edit_category(id):
    cat = Category.query.get_or_404(id)
    if cat.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('categories'))
    if request.method == 'POST':
        cat.name = request.form.get('name', '').strip()
        cat.description = request.form.get('description')
        cat.icon = request.form.get('icon')
        db.session.commit()
        flash('Category updated!', 'success')
        return redirect(url_for('categories'))
    return render_template('edit_category.html', category=cat)


@app.route('/categories/delete/<int:id>', methods=['POST'])
@login_required
@admin_required
def delete_category(id):
    cat = Category.query.get_or_404(id)
    if cat.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('categories'))
    db.session.delete(cat)
    db.session.commit()
    flash('Category deleted.', 'success')
    return redirect(url_for('categories'))


# ========== SUPPLIER MANAGEMENT ==========

@app.route('/suppliers')
@login_required
def suppliers():
    sup_list = Supplier.query.filter_by(company_id=current_user.company_id).all()
    return render_template('suppliers.html', suppliers=sup_list)


@app.route('/suppliers/add', methods=['GET', 'POST'])
@login_required
@manager_or_admin
def add_supplier():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Supplier name is required.', 'danger')
            return render_template('add_supplier.html')

        # Create the supplier object FIRST
        supplier = Supplier(
            name=name,
            contact_person=request.form.get('contact_person', '').strip() or None,
            email=request.form.get('email', '').strip() or None,
            phone=request.form.get('phone', '').strip() or None,
            alternative_phone=request.form.get('alternative_phone', '').strip() or None,
            address=request.form.get('address', '').strip() or None,
            gst_number=request.form.get('gst_number', '').strip() or None,
            payment_terms=request.form.get('payment_terms', '').strip() or None,
            rating=int(request.form.get('rating', 3)),
            main_location=request.form.get('main_location', '').strip() or None,
            sub_location=request.form.get('sub_location', '').strip() or None,
            company_id=current_user.company_id
        )
        
        # Handle installation date
        inst_date_str = request.form.get('installation_date', '').strip()
        if inst_date_str:
            supplier.installation_date = datetime.strptime(inst_date_str, '%Y-%m-%d')
        
        supplier.installation_by = request.form.get('installation_by', '').strip() or None
        supplier.installation_ref_no = request.form.get('installation_ref_no', '').strip() or None
        supplier.installation_notes = request.form.get('installation_notes', '').strip() or None
        supplier.warranty_card_no = request.form.get('warranty_card_no', '').strip() or None
        supplier.notes = request.form.get('notes', '').strip() or None
        
        # Handle installation certificate upload
        if 'installation_certificate' in request.files:
            cert = request.files['installation_certificate']
            if cert and cert.filename and allowed_file(cert.filename):
                safe = secure_filename(cert.filename)
                cert_filename = f"{datetime.utcnow().timestamp()}_{safe}"
                cert.save(os.path.join(app.config['UPLOAD_FOLDER_SUPPLIER_DOCS'], cert_filename))
                supplier.installation_certificate = cert_filename
        
        db.session.add(supplier)
        db.session.flush()  # This gives us supplier.id before commit
        
        # --- Handle Payment if provided ---
        if request.form.get('amount'):
            amount_str = request.form.get('amount', '').strip()
            if amount_str:
                receipt_filename = None
                receipt_original = None
                if 'receipt_file' in request.files:
                    f = request.files['receipt_file']
                    if f and f.filename and allowed_file(f.filename):
                        receipt_original = f.filename
                        safe = secure_filename(f.filename)
                        receipt_filename = f"{datetime.utcnow().timestamp()}_{safe}"
                        f.save(os.path.join(app.config['UPLOAD_FOLDER_SUPPLIER_PAYMENTS'], receipt_filename))
                
                payment = SupplierPayment(
                    supplier_id=supplier.id,
                    payment_date=datetime.strptime(request.form.get('payment_date', ''), '%Y-%m-%d') if request.form.get('payment_date') else datetime.utcnow(),
                    amount=float(amount_str),
                    payment_method=request.form.get('payment_method', 'cash'),
                    upi_id=request.form.get('upi_id') or None,
                    utr_reference=request.form.get('utr_reference') or None,
                    card_last4=request.form.get('card_last4') or None,
                    card_type=request.form.get('card_type') or None,
                    bank_name=request.form.get('bank_name') or None,
                    cheque_number=request.form.get('cheque_number') or None,
                    purpose=request.form.get('purpose') or None,
                    invoice_ref=request.form.get('invoice_ref') or None,
                    receipt_file=receipt_filename,
                    receipt_original_name=receipt_original,
                    created_by=current_user.id
                )
                db.session.add(payment)
        
        # --- Handle Warranty if provided ---
        if request.form.get('warranty_type'):
            doc_filename = None
            doc_original = None
            if 'warranty_document' in request.files:
                f = request.files['warranty_document']
                if f and f.filename and allowed_file(f.filename):
                    doc_original = f.filename
                    safe = secure_filename(f.filename)
                    doc_filename = f"{datetime.utcnow().timestamp()}_{safe}"
                    f.save(os.path.join(app.config['UPLOAD_FOLDER_SUPPLIER_DOCS'], doc_filename))
            
            def parse_warranty_date(field):
                val = request.form.get(field, '').strip()
                return datetime.strptime(val, '%Y-%m-%d') if val else None
            
            warranty = SupplierWarranty(
                supplier_id=supplier.id,
                warranty_type=request.form.get('warranty_type'),
                issuer_company=request.form.get('issuer_company') or None,
                issuer_address=request.form.get('issuer_address') or None,
                issuer_phone=request.form.get('issuer_phone') or None,
                issuer_email=request.form.get('issuer_email') or None,
                policy_number=request.form.get('policy_number') or None,
                start_date=parse_warranty_date('warranty_start'),
                expiry_date=parse_warranty_date('warranty_expiry'),
                coverage_amount=float(request.form.get('coverage_amount')) if request.form.get('coverage_amount') else None,
                document_file=doc_filename,
                document_original_name=doc_original
            )
            db.session.add(warranty)
        
        # --- Handle AMC if provided ---
        if request.form.get('amc_company'):
            doc_filename = None
            doc_original = None
            if 'amc_document' in request.files:
                f = request.files['amc_document']
                if f and f.filename and allowed_file(f.filename):
                    doc_original = f.filename
                    safe = secure_filename(f.filename)
                    doc_filename = f"{datetime.utcnow().timestamp()}_{safe}"
                    f.save(os.path.join(app.config['UPLOAD_FOLDER_SUPPLIER_DOCS'], doc_filename))
            
            def parse_amc_date(field):
                val = request.form.get(field, '').strip()
                return datetime.strptime(val, '%Y-%m-%d') if val else None
            
            amc = SupplierAMC(
                supplier_id=supplier.id,
                amc_company=request.form.get('amc_company'),
                contract_number=request.form.get('amc_contract_number') or None,
                start_date=parse_amc_date('amc_start'),
                end_date=parse_amc_date('amc_end'),
                first_service_due=parse_amc_date('first_service_due'),
                contract_value=float(request.form.get('amc_value')) if request.form.get('amc_value') else None,
                document_file=doc_filename,
                document_original_name=doc_original
            )
            db.session.add(amc)
        
        db.session.commit()
        log_activity(current_user.id, 'add_supplier', f'Added supplier: {supplier.name}')
        flash('Supplier added with all details!', 'success')
        return redirect(url_for('suppliers'))
    
    return render_template('add_supplier.html')


@app.route('/suppliers/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@manager_or_admin
def edit_supplier(id):
    supplier = Supplier.query.get_or_404(id)
    if supplier.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('suppliers'))
 
    if request.method == 'POST':
        supplier.name = request.form.get('name', '').strip()
        supplier.contact_person = request.form.get('contact_person')
        supplier.email = request.form.get('email')
        supplier.phone = request.form.get('phone')
        supplier.alternative_phone = request.form.get('alternative_phone')
        supplier.address = request.form.get('address')
        supplier.gst_number = request.form.get('gst_number')
        supplier.payment_terms = request.form.get('payment_terms')
        supplier.rating = int(request.form.get('rating', 3))
        supplier.main_location = request.form.get('main_location')
        supplier.sub_location = request.form.get('sub_location')
        inst_date_str = request.form.get('installation_date', '').strip()
        supplier.installation_date = datetime.strptime(inst_date_str, '%Y-%m-%d') if inst_date_str else None
        supplier.installation_by = request.form.get('installation_by')
        supplier.installation_ref_no = request.form.get('installation_ref_no')
        supplier.installation_notes = request.form.get('installation_notes')
        supplier.warranty_card_no = request.form.get('warranty_card_no')
        supplier.notes = request.form.get('notes')
 
        # Installation certificate upload (replace if new file provided)
        if 'installation_certificate' in request.files:
            cert = request.files['installation_certificate']
            if cert and cert.filename and allowed_file(cert.filename):
                safe = secure_filename(cert.filename)
                cert_filename = f"{datetime.utcnow().timestamp()}_{safe}"
                cert.save(os.path.join(app.config['UPLOAD_FOLDER_SUPPLIER_DOCS'], cert_filename))
                supplier.installation_certificate = cert_filename
 
        db.session.commit()
        log_activity(current_user.id, 'edit_supplier', f'Edited supplier: {supplier.name}')
        flash('Supplier updated!', 'success')
        return redirect(url_for('edit_supplier', id=id))
 
    return render_template('edit_supplier.html', supplier=supplier)


@app.route('/suppliers/delete/<int:id>', methods=['POST'])
@login_required
@admin_required
def delete_supplier(id):
    supplier = Supplier.query.get_or_404(id)
    if supplier.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('suppliers'))
    log_activity(current_user.id, 'delete_supplier', f'Deleted supplier: {supplier.name}')
    db.session.delete(supplier)
    db.session.commit()
    flash('Supplier deleted.', 'success')
    return redirect(url_for('suppliers'))

@app.route('/suppliers/<int:supplier_id>/payments/add', methods=['POST'])
@login_required
@manager_or_admin
def add_supplier_payment(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    if supplier.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('suppliers'))
 
    amount_str = request.form.get('amount', '').strip()
    if not amount_str:
        flash('Amount is required.', 'danger')
        return redirect(url_for('edit_supplier', id=supplier_id))
 
    # Receipt file upload
    receipt_filename = None
    receipt_original = None
    if 'receipt_file' in request.files:
        f = request.files['receipt_file']
        if f and f.filename and allowed_file(f.filename):
            receipt_original = f.filename
            safe = secure_filename(f.filename)
            receipt_filename = f"{datetime.utcnow().timestamp()}_{safe}"
            f.save(os.path.join(app.config['UPLOAD_FOLDER_SUPPLIER_PAYMENTS'], receipt_filename))
 
    pay_date_str = request.form.get('payment_date', '').strip()
    pay_date = datetime.strptime(pay_date_str, '%Y-%m-%d') if pay_date_str else datetime.utcnow()
 
    payment = SupplierPayment(
        supplier_id=supplier_id,
        payment_date=pay_date,
        amount=float(amount_str),
        currency=request.form.get('currency', 'INR'),
        payment_method=request.form.get('payment_method', 'cash'),
        upi_id=request.form.get('upi_id') or None,
        utr_reference=request.form.get('utr_reference') or None,
        card_last4=request.form.get('card_last4') or None,
        card_type=request.form.get('card_type') or None,
        bank_name=request.form.get('bank_name') or None,
        cheque_number=request.form.get('cheque_number') or None,
        account_number=request.form.get('account_number') or None,
        ifsc_code=request.form.get('ifsc_code') or None,
        purpose=request.form.get('purpose') or None,
        invoice_ref=request.form.get('invoice_ref') or None,
        notes=request.form.get('notes') or None,
        receipt_file=receipt_filename,
        receipt_original_name=receipt_original,
        created_by=current_user.id
    )
    db.session.add(payment)
    db.session.commit()
    log_activity(current_user.id, 'add_supplier_payment',
                 f'Payment ₹{amount_str} to supplier {supplier.name}')
    flash('Payment recorded!', 'success')
    return redirect(url_for('edit_supplier', id=supplier_id) + '#payments')
 
 
@app.route('/suppliers/payments/delete/<int:payment_id>', methods=['POST'])
@login_required
@admin_required
def delete_supplier_payment(payment_id):
    payment = SupplierPayment.query.get_or_404(payment_id)
    supplier_id = payment.supplier_id
    supplier = Supplier.query.get_or_404(supplier_id)
    if supplier.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('suppliers'))
    db.session.delete(payment)
    db.session.commit()
    flash('Payment deleted.', 'success')
    return redirect(url_for('edit_supplier', id=supplier_id) + '#payments')

@app.route('/suppliers/<int:supplier_id>/warranties/add', methods=['POST'])
@login_required
@manager_or_admin
def add_supplier_warranty(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    if supplier.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('suppliers'))
 
    doc_filename = None
    doc_original = None
    if 'document_file' in request.files:
        f = request.files['document_file']
        if f and f.filename and allowed_file(f.filename):
            doc_original = f.filename
            safe = secure_filename(f.filename)
            doc_filename = f"{datetime.utcnow().timestamp()}_{safe}"
            f.save(os.path.join(app.config['UPLOAD_FOLDER_SUPPLIER_DOCS'], doc_filename))
 
    def _parse_date(field):
        val = request.form.get(field, '').strip()
        return datetime.strptime(val, '%Y-%m-%d') if val else None
 
    warranty = SupplierWarranty(
        supplier_id=supplier_id,
        warranty_type=request.form.get('warranty_type', 'product_warranty'),
        issuer_company=request.form.get('issuer_company') or None,
        issuer_address=request.form.get('issuer_address') or None,
        issuer_phone=request.form.get('issuer_phone') or None,
        issuer_email=request.form.get('issuer_email') or None,
        issuer_website=request.form.get('issuer_website') or None,
        policy_number=request.form.get('policy_number') or None,
        certificate_number=request.form.get('certificate_number') or None,
        start_date=_parse_date('start_date'),
        expiry_date=_parse_date('expiry_date'),
        coverage_amount=float(request.form.get('coverage_amount') or 0) or None,
        premium_amount=float(request.form.get('premium_amount') or 0) or None,
        deductible=float(request.form.get('deductible') or 0) or None,
        coverage_details=request.form.get('coverage_details') or None,
        notes=request.form.get('notes') or None,
        document_file=doc_filename,
        document_original_name=doc_original,
    )
    db.session.add(warranty)
    db.session.commit()
    log_activity(current_user.id, 'add_supplier_warranty',
                 f'Warranty/Insurance added for supplier {supplier.name}')
    flash('Warranty / Insurance record saved!', 'success')
    return redirect(url_for('edit_supplier', id=supplier_id) + '#warranty')
 
 
@app.route('/suppliers/warranties/delete/<int:warranty_id>', methods=['POST'])
@login_required
@admin_required
def delete_supplier_warranty(warranty_id):
    w = SupplierWarranty.query.get_or_404(warranty_id)
    supplier_id = w.supplier_id
    supplier = Supplier.query.get_or_404(supplier_id)
    if supplier.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('suppliers'))
    db.session.delete(w)
    db.session.commit()
    flash('Record deleted.', 'success')
    return redirect(url_for('edit_supplier', id=supplier_id) + '#warranty')
 
 
# ---------------------------------------------------------------------------
# 7.  NEW — Supplier AMC routes
# ---------------------------------------------------------------------------
@app.route('/suppliers/<int:supplier_id>/amcs/add', methods=['POST'])
@login_required
@manager_or_admin
def add_supplier_amc(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    if supplier.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('suppliers'))
 
    doc_filename = None
    doc_original = None
    if 'document_file' in request.files:
        f = request.files['document_file']
        if f and f.filename and allowed_file(f.filename):
            doc_original = f.filename
            safe = secure_filename(f.filename)
            doc_filename = f"{datetime.utcnow().timestamp()}_{safe}"
            f.save(os.path.join(app.config['UPLOAD_FOLDER_SUPPLIER_DOCS'], doc_filename))
 
    def _parse_date(field):
        val = request.form.get(field, '').strip()
        return datetime.strptime(val, '%Y-%m-%d') if val else None
 
    def _int(field, default=None):
        val = request.form.get(field, '').strip()
        return int(val) if val.isdigit() else default
 
    amc = SupplierAMC(
        supplier_id=supplier_id,
        amc_company=request.form.get('amc_company') or None,
        amc_address=request.form.get('amc_address') or None,
        amc_phone=request.form.get('amc_phone') or None,
        amc_email=request.form.get('amc_email') or None,
        amc_contact_person=request.form.get('amc_contact_person') or None,
        contract_number=request.form.get('contract_number') or None,
        contract_type=request.form.get('contract_type', 'comprehensive'),
        start_date=_parse_date('start_date'),
        end_date=_parse_date('end_date'),
        contract_value=float(request.form.get('contract_value') or 0) or None,
        first_service_due=_parse_date('first_service_due'),
        service_interval_months=_int('service_interval_months', 3),
        visits_per_year=_int('visits_per_year'),
        response_time_hours=_int('response_time_hours'),
        escalation_contact=request.form.get('escalation_contact') or None,
        notes=request.form.get('notes') or None,
        document_file=doc_filename,
        document_original_name=doc_original,
    )
    db.session.add(amc)
    db.session.commit()
    log_activity(current_user.id, 'add_supplier_amc',
                 f'AMC added for supplier {supplier.name}')
    flash('AMC record saved!', 'success')
    return redirect(url_for('edit_supplier', id=supplier_id) + '#amc')
 
 
@app.route('/suppliers/amcs/delete/<int:amc_id>', methods=['POST'])
@login_required
@admin_required
def delete_supplier_amc(amc_id):
    amc = SupplierAMC.query.get_or_404(amc_id)
    supplier_id = amc.supplier_id
    supplier = Supplier.query.get_or_404(supplier_id)
    if supplier.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('suppliers'))
    db.session.delete(amc)
    db.session.commit()
    flash('AMC record deleted.', 'success')
    return redirect(url_for('edit_supplier', id=supplier_id) + '#amc')
 
 
# ---------------------------------------------------------------------------
# 8.  NEW — Fuel Log routes  (attached to InventoryItem, not Supplier)
# ---------------------------------------------------------------------------
@app.route('/inventory/<int:item_id>/fuel/add', methods=['POST'])
@login_required
def add_fuel_log(item_id):
    item = InventoryItem.query.get_or_404(item_id)
    if item.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('inventory'))
 
    # Only valid for vehicle categories
    if not item.is_vehicle:
        flash('Fuel logs are only available for vehicle assets.', 'danger')
        return redirect(url_for('view_asset', item_id=item_id))
 
    receipt_filename = None
    receipt_original = None
    if 'receipt_photo' in request.files:
        f = request.files['receipt_photo']
        if f and f.filename and allowed_file(f.filename):
            receipt_original = f.filename
            safe = secure_filename(f.filename)
            receipt_filename = f"{datetime.utcnow().timestamp()}_{safe}"
            f.save(os.path.join(app.config['UPLOAD_FOLDER_FUEL_RECEIPTS'], receipt_filename))
 
    def _parse_date(field):
        val = request.form.get(field, '').strip()
        return datetime.strptime(val, '%Y-%m-%d') if val else datetime.utcnow()
 
    def _float(field):
        val = request.form.get(field, '').strip()
        try:
            return float(val) if val else None
        except ValueError:
            return None
 
    def _int(field):
        val = request.form.get(field, '').strip()
        return int(val) if val.isdigit() else None
 
    fuel_log = FuelLog(
        item_id=item_id,
        fill_date=_parse_date('fill_date'),
        fuel_type=request.form.get('fuel_type', 'petrol'),
        quantity_litres=_float('quantity_litres'),
        quantity_unit=request.form.get('quantity_unit', 'litres'),
        cost_per_unit=_float('cost_per_unit'),
        total_cost=_float('total_cost'),
        odometer_km=_int('odometer_km'),
        mileage_kmpl=_float('mileage_kmpl'),
        station_name=request.form.get('station_name') or None,
        station_location=request.form.get('station_location') or None,
        filled_by=request.form.get('filled_by') or current_user.username,
        is_full_tank=request.form.get('is_full_tank') == 'on',
        receipt_photo=receipt_filename,
        receipt_original_name=receipt_original,
        notes=request.form.get('notes') or None,
        created_by=current_user.id
    )
    db.session.add(fuel_log)
    db.session.commit()
    log_activity(current_user.id, 'add_fuel_log',
                 f'Fuel log added for asset {item.name}')
    flash('Fuel log recorded!', 'success')
    return redirect(url_for('view_asset', item_id=item_id) + '#tab-fuel')
 
 
@app.route('/inventory/fuel/delete/<int:log_id>', methods=['POST'])
@login_required
def delete_fuel_log(log_id):
    log = FuelLog.query.get_or_404(log_id)
    item_id = log.item_id
    item = InventoryItem.query.get_or_404(item_id)
    if item.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('inventory'))
    db.session.delete(log)
    db.session.commit()
    flash('Fuel log deleted.', 'success')
    return redirect(url_for('view_asset', item_id=item_id) + '#tab-fuel')

# ========== MAINTENANCE MANAGEMENT ==========

@app.route('/maintenance')
@login_required
def maintenance():
    schedules = MaintenanceSchedule.query.join(InventoryItem).filter(
        InventoryItem.company_id == current_user.company_id
    ).order_by(MaintenanceSchedule.due_date).all()
    return render_template('maintenance.html', schedules=schedules)


@app.route('/maintenance/schedule/<int:item_id>', methods=['POST'])
@login_required
def schedule_maintenance(item_id):
    item = InventoryItem.query.get_or_404(item_id)
    if item.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('inventory'))

    due_date_str = request.form.get('due_date', '').strip()
    if not due_date_str:
        flash('Due date is required.', 'danger')
        return redirect(url_for('inventory'))

    due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
    frequency_str = request.form.get('frequency_days', '').strip()
    frequency = int(frequency_str) if frequency_str.isdigit() else None

    schedule = MaintenanceSchedule(
        item_id=item_id,
        due_date=due_date,
        frequency_days=frequency,
        notes=request.form.get('notes'),
        assigned_to=int(request.form['assigned_to']) if request.form.get('assigned_to') else None,
        priority=request.form.get('priority', 'normal')
    )
    db.session.add(schedule)
    db.session.commit()

    # Seed checklist from template (NEW)
    seed_checklist_from_template(schedule, item)
    db.session.commit()

    log_activity(current_user.id, 'schedule_maintenance',
                 f'Scheduled maintenance for item {item.name}')
    flash('Maintenance scheduled successfully!', 'success')
    return redirect(url_for('maintenance'))


@app.route('/maintenance/perform/<int:schedule_id>', methods=['GET', 'POST'])
@login_required
def perform_maintenance(schedule_id):
    schedule = MaintenanceSchedule.query.get_or_404(schedule_id)
    checklist_items = MaintenanceChecklist.query.filter_by(
        maintenance_schedule_id=schedule_id
    ).all()

    if request.method == 'POST':
        for ci in checklist_items:
            is_completed = request.form.get(f'checklist_{ci.id}') == 'on'
            if is_completed and not ci.is_completed:
                ci.is_completed = True
                ci.completed_at = datetime.utcnow()
                ci.completed_by = current_user.id
                ci.remarks = request.form.get(f'remarks_{ci.id}')

        cost = float(request.form.get('cost', 0) or 0)
        service_log = ServiceLog(
            item_id=schedule.item_id,
            maintenance_schedule_id=schedule.id,
            performed_by=request.form.get('performed_by', current_user.username),
            actions=request.form.get('actions', ''),
            parts_used=request.form.get('parts_used'),
            cost=cost,
            notes=request.form.get('notes')
        )

        if schedule.frequency_days:
            service_log.next_service_due = datetime.utcnow() + timedelta(days=schedule.frequency_days)
            schedule.due_date = service_log.next_service_due
            schedule.status = 'pending'
            # Seed fresh checklist for next cycle
            seed_checklist_from_template(schedule, schedule.item)
        else:
            schedule.status = 'completed'

        schedule.last_performed = datetime.utcnow()
        schedule.total_cost = (schedule.total_cost or 0) + cost

        db.session.add(service_log)
        db.session.commit()

        log_activity(current_user.id, 'perform_maintenance',
                     f'Performed maintenance for item {schedule.item_id}')
        flash('Maintenance completed and logged!', 'success')
        return redirect(url_for('maintenance'))

    return render_template('perform_maintenance.html',
                           schedule=schedule, checklist_items=checklist_items)


@app.route('/maintenance/complete/<int:id>', methods=['POST'])
@login_required
def complete_maintenance(id):
    maint = MaintenanceSchedule.query.get_or_404(id)
    maint.last_performed = datetime.utcnow()
    if maint.frequency_days:
        maint.due_date = datetime.utcnow() + timedelta(days=maint.frequency_days)
        maint.status = 'pending'
    else:
        maint.status = 'completed'
    db.session.commit()
    log_activity(current_user.id, 'complete_maintenance',
                 f'Completed maintenance for item {maint.item_id}')
    flash('Maintenance marked as completed!', 'success')
    return redirect(url_for('maintenance'))


# ========== MAINTENANCE TEMPLATES ==========

@app.route('/maintenance/templates')
@login_required
@manager_or_admin
def maintenance_templates():
    templates = MaintenanceTemplate.query.filter_by(
        company_id=current_user.company_id
    ).all()
    categories = Category.query.filter_by(company_id=current_user.company_id).all()
    return render_template('maintenance_templates.html',
                           templates=templates, categories=categories)


@app.route('/maintenance/templates/add', methods=['POST'])
@login_required
@manager_or_admin
def add_maintenance_template():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Template name is required.', 'danger')
        return redirect(url_for('maintenance_templates'))
    template = MaintenanceTemplate(
        name=name,
        category_id=int(request.form['category_id']) if request.form.get('category_id') else None,
        company_id=current_user.company_id
    )
    db.session.add(template)
    db.session.commit()

    tasks = request.form.getlist('task_name')
    for idx, task in enumerate(tasks):
        if task.strip():
            db.session.add(MaintenanceChecklistTemplate(
                template_id=template.id,
                task_name=task.strip(),
                instructions=request.form.getlist('instructions')[idx] if request.form.getlist('instructions') else '',
                is_critical='critical_' + str(idx) in request.form,
                sort_order=idx
            ))
    db.session.commit()
    flash('Template created!', 'success')
    return redirect(url_for('maintenance_templates'))


# ========== SERVICE LOGS ==========

@app.route('/service-logs')
@login_required
def service_logs():
    # FIX: filter by company — no cross-tenant data leak
    logs = ServiceLog.query.join(InventoryItem).filter(
        InventoryItem.company_id == current_user.company_id
    ).order_by(ServiceLog.service_date.desc()).all()
    return render_template('service_logs.html', logs=logs)


@app.route('/service-log/add/<int:item_id>', methods=['POST'])
@login_required
def add_service_log(item_id):
    item = InventoryItem.query.get_or_404(item_id)
    if item.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('inventory'))

    def parse_date(field):
        val = request.form.get(field, '').strip()
        return datetime.strptime(val, '%Y-%m-%d') if val else None

    service_log = ServiceLog(
        item_id=item_id,
        service_date=parse_date('service_date') or datetime.utcnow(),
        performed_by=request.form.get('performed_by', '').strip(),
        actions=request.form.get('actions', ''),
        parts_used=request.form.get('parts_used'),
        cost=float(request.form.get('cost') or 0),
        next_service_due=parse_date('next_service_due'),
        notes=request.form.get('notes')
    )
    db.session.add(service_log)
    db.session.commit()
    log_activity(current_user.id, 'add_service_log',
                 f'Added service log for item {item.name}')
    flash('Service log added successfully!', 'success')
    return redirect(url_for('view_asset', item_id=item_id))


# ========== ALERTS ==========

@app.route('/alerts')
@login_required
def alerts():
    alert_list = Alert.query.join(InventoryItem).filter(
        InventoryItem.company_id == current_user.company_id
    ).order_by(Alert.triggered_at.desc()).all()
    return render_template('alerts.html', alerts=alert_list)


@app.route('/alerts/resolve/<int:id>', methods=['POST'])
@login_required
def resolve_alert(id):
    alert = Alert.query.get_or_404(id)
    alert.is_read = True
    alert.resolved_at = datetime.utcnow()
    alert.resolved_by = current_user.id
    db.session.commit()
    log_activity(current_user.id, 'resolve_alert', f'Resolved alert: {alert.message}')
    flash('Alert resolved!', 'success')
    return redirect(url_for('alerts'))


@app.route('/api/alerts/check')
@login_required
def api_check_alerts():
    """FIX: was calling undefined check_alerts() — now calls check_all_alerts()."""
    with app.app_context():
        check_all_alerts()
    open_alerts = Alert.query.join(InventoryItem).filter(
        InventoryItem.company_id == current_user.company_id,
        Alert.resolved_at.is_(None)
    ).all()
    return jsonify({
        'count': len(open_alerts),
        'alerts': [{'id': a.id, 'type': a.alert_type, 'message': a.message} for a in open_alerts]
    })


# ========== ALERT SETTINGS ==========

@app.route('/alert-settings', methods=['GET', 'POST'])
@login_required
@manager_or_admin
def alert_settings():
    alert_types = ['warranty', 'maintenance', 'insurance', 'low_stock', 'amc', 'high_value']

    if request.method == 'POST':
        for alert_type in alert_types:
            setting = AlertSetting.query.filter_by(
                company_id=current_user.company_id, alert_type=alert_type
            ).first()
            if not setting:
                setting = AlertSetting(
                    company_id=current_user.company_id, alert_type=alert_type
                )
            setting.days_before = int(request.form.get(f'{alert_type}_days', 30))
            setting.escalation_days = int(request.form.get(f'{alert_type}_escalation', 7))
            setting.send_email = request.form.get(f'{alert_type}_email') == 'on'
            setting.send_whatsapp = request.form.get(f'{alert_type}_whatsapp') == 'on'
            setting.send_sms = request.form.get(f'{alert_type}_sms') == 'on'
            setting.is_active = request.form.get(f'{alert_type}_active') == 'on'
            db.session.add(setting)
        db.session.commit()
        flash('Alert settings updated!', 'success')
        return redirect(url_for('alert_settings'))

    settings = {}
    for alert_type in alert_types:
        setting = AlertSetting.query.filter_by(
            company_id=current_user.company_id, alert_type=alert_type
        ).first()
        settings[alert_type] = setting or AlertSetting(alert_type=alert_type, days_before=30)
    return render_template('alert_settings.html', settings=settings, alert_types=alert_types)


# ========== USER MANAGEMENT (admin only) ==========

@app.route('/users')
@login_required
@admin_required
def users():
    user_list = User.query.filter_by(company_id=current_user.company_id).all()
    return render_template('users.html', users=user_list)


@app.route('/users/add', methods=['GET', 'POST'])
@login_required
@admin_required
def add_user():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'user')

        errors = []
        if not username:
            errors.append('Username is required.')
        if not email:
            errors.append('Email is required.')
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        if User.query.filter_by(username=username).first():
            errors.append('Username already exists.')
        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('add_user.html')

        user = User(
            username=username,
            email=email,
            phone=request.form.get('phone'),
            role=role,
            company_id=current_user.company_id,
            is_active=True
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        log_activity(current_user.id, 'add_user', f'Created user: {username} ({role})')
        flash('User created successfully!', 'success')
        return redirect(url_for('users'))
    return render_template('add_user.html')


@app.route('/users/toggle/<int:id>', methods=['POST'])
@login_required
@admin_required
def toggle_user(id):
    user = User.query.get_or_404(id)
    if user.id == current_user.id:
        flash('You cannot deactivate yourself.', 'danger')
        return redirect(url_for('users'))
    user.is_active = not user.is_active
    db.session.commit()
    status = 'activated' if user.is_active else 'deactivated'
    flash(f'User {user.username} {status}.', 'success')
    return redirect(url_for('users'))


# ========== QR CODE & BARCODE ==========

@app.route('/generate-barcode/<int:item_id>')
@login_required
def generate_barcode(item_id):
    item = InventoryItem.query.get_or_404(item_id)
    if item.company_id != current_user.company_id:
        flash('Access denied.', 'danger')
        return redirect(url_for('inventory'))

    barcode_data = {
        'asset_code': item.asset_code,
        'name': item.name,
        'serial_number': item.serial_number,
        'brand': item.brand,
        'model': item.model
    }
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(json.dumps(barcode_data))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()
    return render_template('barcode.html', item=item, barcode_img=img_str)


@app.route('/scan-barcode', methods=['GET', 'POST'])
@login_required
def scan_barcode():
    if request.method == 'POST':
        asset_code = request.form.get('asset_code', '').strip()
        item = InventoryItem.query.filter_by(
            asset_code=asset_code, company_id=current_user.company_id
        ).first()
        if item:
            flash(f'Asset found: {item.name}', 'success')
            return redirect(url_for('view_asset', item_id=item.id))
        flash('Asset not found!', 'danger')
    return render_template('scan_barcode.html')


@app.route('/api/search-by-barcode')
@login_required
def search_by_barcode():
    asset_code = request.args.get('asset_code', '').strip()
    item = InventoryItem.query.filter_by(
        asset_code=asset_code, company_id=current_user.company_id
    ).first()
    if item:
        return jsonify({
            'found': True,
            'asset_code': item.asset_code,
            'name': item.name,
            'serial_number': item.serial_number,
            'brand': item.brand,
            'model': item.model,
            'location': item.location,
            'condition': item.condition
        })
    return jsonify({'found': False})


# ========== REPORTS ==========

@app.route('/reports')
@login_required
def reports():
    return render_template('reports.html')


@app.route('/reports/inventory-excel')
@login_required
def export_inventory_excel():
    items = InventoryItem.query.filter_by(company_id=current_user.company_id).all()
    filename = f"inventory_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    filepath = os.path.join('static/reports', filename)
    report_gen.generate_inventory_excel(items, filepath)
    log_activity(current_user.id, 'export_report', 'Exported inventory Excel report')
    return send_file(filepath, as_attachment=True)


@app.route('/reports/maintenance-pdf')
@login_required
def export_maintenance_pdf():
    maintenance = MaintenanceSchedule.query.join(InventoryItem).filter(
        InventoryItem.company_id == current_user.company_id
    ).all()
    filename = f"maintenance_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    filepath = os.path.join('static/reports', filename)
    report_gen.generate_maintenance_pdf(maintenance, filepath)
    log_activity(current_user.id, 'export_report', 'Exported maintenance PDF report')
    return send_file(filepath, as_attachment=True)


# ========== PWA MANIFEST ==========

@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "House Inventory System",
        "short_name": "Inventory Pro",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#3498db",
        "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"}]
    })


# ========== INITIAL SETUP (runs once at startup, not per request) ==========

if __name__ == '__main__':
    with app.app_context():
        #db.drop_all()
        db.create_all()
        _create_initial_data()
    app.run(debug=True, host='0.0.0.0', port=5000)
