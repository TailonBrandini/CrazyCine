from datetime import datetime, timedelta
from functools import wraps
import os
import uuid

import mercadopago
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = 'troque-essa-chave-secreta'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///crazycine.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

MERCADO_PAGO_ACCESS_TOKEN = os.getenv('MERCADO_PAGO_ACCESS_TOKEN', '')
WEBHOOK_BASE_URL = os.getenv('WEBHOOK_BASE_URL', '')
# Para apresentação/testes do trabalho, todo Pix é gerado com R$ 0,01.
# Em produção, troque para False para usar o preço real do filme.
USE_DEMO_PIX_VALUE = os.getenv('USE_DEMO_PIX_VALUE', 'true').lower() == 'true'
DEMO_PIX_VALUE = float(os.getenv('DEMO_PIX_VALUE', '0.01'))

db = SQLAlchemy(app)

list_movies = db.Table(
    'list_movies',
    db.Column('list_id', db.Integer, db.ForeignKey('movie_list.id'), primary_key=True),
    db.Column('movie_id', db.Integer, db.ForeignKey('movie.id'), primary_key=True)
)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    lists = db.relationship('MovieList', backref='user', lazy=True, cascade='all, delete-orphan')
    transactions = db.relationship('Transaction', backref='user', lazy=True, cascade='all, delete-orphan')

class Movie(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=False)
    genre = db.Column(db.String(80), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    director = db.Column(db.String(120), nullable=False)
    poster_url = db.Column(db.String(300), nullable=True)
    rent_price = db.Column(db.Float, nullable=False, default=7.90)
    buy_price = db.Column(db.Float, nullable=False, default=29.90)

class MovieList(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    movies = db.relationship('Movie', secondary=list_movies, lazy='subquery', backref=db.backref('movie_lists', lazy=True))

class WatchedMovie(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    movie_id = db.Column(db.Integer, db.ForeignKey('movie.id'), nullable=False)
    movie = db.relationship('Movie')

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    movie_id = db.Column(db.Integer, db.ForeignKey('movie.id'), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # aluguel ou compra
    price = db.Column(db.Float, nullable=False)
    payment_method = db.Column(db.String(40), nullable=False, default='pix')
    status = db.Column(db.String(30), nullable=False, default='pendente')
    mp_payment_id = db.Column(db.String(80), nullable=True, unique=True)
    pix_qr_code = db.Column(db.Text, nullable=True)
    pix_qr_code_base64 = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    movie = db.relationship('Movie')

    @property
    def is_released(self):
        if self.status != 'aprovado':
            return False
        if self.type == 'compra':
            return True
        return self.expires_at and self.expires_at > datetime.utcnow()


def login_required(route):
    @wraps(route)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            flash('Faça login para acessar esta página.', 'warning')
            return redirect(url_for('login'))
        return route(*args, **kwargs)
    return wrapper


def current_user():
    if 'user_id' in session:
        return User.query.get(session['user_id'])
    return None


@app.context_processor
def inject_user():
    return dict(current_user=current_user())


@app.template_filter('brl')
def brl(value):
    return f'R$ {value:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')



def get_mp_sdk():
    if not MERCADO_PAGO_ACCESS_TOKEN:
        return None
    return mercadopago.SDK(MERCADO_PAGO_ACCESS_TOKEN)


def get_webhook_url():
    if WEBHOOK_BASE_URL:
        return WEBHOOK_BASE_URL.rstrip('/') + url_for('mercado_pago_webhook')
    return None


def create_pix_payment(transaction, user):
    sdk = get_mp_sdk()
    if not sdk:
        raise RuntimeError('Configure a variável de ambiente MERCADO_PAGO_ACCESS_TOKEN antes de gerar Pix real.')

    amount = DEMO_PIX_VALUE if USE_DEMO_PIX_VALUE else transaction.price
    payment_data = {
        'transaction_amount': float(amount),
        'description': f'CrazyCine - {transaction.type} - {transaction.movie.title}',
        'payment_method_id': 'pix',
        'external_reference': str(transaction.id),
        'payer': {
            'email': user.email,
            'first_name': user.name.split()[0] if user.name else 'Cliente'
        }
    }
    webhook_url = get_webhook_url()
    if webhook_url:
        payment_data['notification_url'] = webhook_url

    request_options = mercadopago.config.RequestOptions()
    request_options.custom_headers = {'x-idempotency-key': str(uuid.uuid4())}
    result = sdk.payment().create(payment_data, request_options)
    response = result.get('response', {})

    if result.get('status') not in [200, 201] or 'id' not in response:
        raise RuntimeError(f'Erro ao criar pagamento Pix: {response}')

    poi = response.get('point_of_interaction', {})
    tx_data = poi.get('transaction_data', {})
    transaction.mp_payment_id = str(response.get('id'))
    transaction.status = 'pendente'
    transaction.pix_qr_code = tx_data.get('qr_code')
    transaction.pix_qr_code_base64 = tx_data.get('qr_code_base64')
    db.session.commit()
    return transaction


def approve_transaction(transaction):
    if transaction.status == 'aprovado':
        return
    transaction.status = 'aprovado'
    if transaction.type == 'aluguel' and not transaction.expires_at:
        transaction.expires_at = datetime.utcnow() + timedelta(days=2)
    db.session.commit()


def sync_payment_status(mp_payment_id):
    sdk = get_mp_sdk()
    if not sdk:
        return None
    result = sdk.payment().get(mp_payment_id)
    response = result.get('response', {})
    status = response.get('status')
    transaction = Transaction.query.filter_by(mp_payment_id=str(mp_payment_id)).first()
    if transaction and status == 'approved':
        approve_transaction(transaction)
    elif transaction and status in ['rejected', 'cancelled', 'refunded', 'charged_back']:
        transaction.status = status
        db.session.commit()
    return transaction


def ensure_schema_columns():
    # Pequena migração automática para bancos antigos do trabalho.
    with db.engine.connect() as conn:
        cols = [row[1] for row in conn.exec_driver_sql('PRAGMA table_info("transaction")').fetchall()]
        needed = {
            'status': 'ALTER TABLE "transaction" ADD COLUMN status VARCHAR(30) DEFAULT "aprovado" NOT NULL',
            'mp_payment_id': 'ALTER TABLE "transaction" ADD COLUMN mp_payment_id VARCHAR(80)',
            'pix_qr_code': 'ALTER TABLE "transaction" ADD COLUMN pix_qr_code TEXT',
            'pix_qr_code_base64': 'ALTER TABLE "transaction" ADD COLUMN pix_qr_code_base64 TEXT',
        }
        for col, sql in needed.items():
            if col not in cols:
                conn.exec_driver_sql(sql)
        conn.commit()

def seed_movies():
    movies = [
        ('Interestelar', 'Ficção Científica', 2014, 'Christopher Nolan', 'https://image.tmdb.org/t/p/w500/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg', 'Um grupo de astronautas viaja por um buraco de minhoca em busca de um novo lar para a humanidade.', 8.90, 34.90),
        ('A Origem', 'Ação / Ficção Científica', 2010, 'Christopher Nolan', 'https://image.tmdb.org/t/p/w500/9e3Dz7aCANy5aRUQF745IlNloJ1.jpg', 'Um ladrão especializado em roubar segredos através dos sonhos recebe uma missão quase impossível.', 7.90, 29.90),
        ('Cidade de Deus', 'Drama / Crime', 2002, 'Fernando Meirelles', 'https://image.tmdb.org/t/p/w500/k7eYdWvhYQyRQoU2TB2A2Xu2TfD.jpg', 'A trajetória de jovens envolvidos com o crime em uma comunidade do Rio de Janeiro.', 6.90, 24.90),
        ('O Senhor dos Anéis: A Sociedade do Anel', 'Fantasia / Aventura', 2001, 'Peter Jackson', 'https://image.tmdb.org/t/p/w500/omoMXT3Z7XrQwRZ2OGJGNWbdeEl.jpg', 'Um hobbit recebe a missão de destruir um anel poderoso antes que ele caia em mãos erradas.', 8.90, 39.90),
        ('Vingadores: Ultimato', 'Ação / Super-herói', 2019, 'Anthony e Joe Russo', 'https://image.tmdb.org/t/p/w500/q6725aR8Zs4IwGMXzZT8aC8lh41.jpg', 'Os heróis sobreviventes tentam reverter os danos causados por Thanos.', 9.90, 44.90),
        ('Divertida Mente', 'Animação / Família', 2015, 'Pete Docter', 'https://image.tmdb.org/t/p/w500/62SAZfLfzhxJWUFJvfIPMw6QUpE.jpg', 'As emoções de uma garota precisam lidar com grandes mudanças em sua vida.', 7.90, 29.90),
        ('Coringa', 'Drama / Suspense', 2019, 'Todd Phillips', 'https://image.tmdb.org/t/p/w500/xLxgVxFWvb9hhUyCDDXxRPPnFck.jpg', 'A origem dramática de Arthur Fleck e sua transformação no vilão Coringa.', 7.90, 29.90),
        ('Matrix', 'Ficção Científica / Ação', 1999, 'Lana e Lilly Wachowski', 'https://image.tmdb.org/t/p/w500/f89U3ADr1oiB1s9GkdPOEpXUk5H.jpg', 'Um programador descobre que a realidade em que vive é uma simulação criada por máquinas.', 6.90, 24.90),
        ('Toy Story', 'Animação / Aventura', 1995, 'John Lasseter', 'https://image.tmdb.org/t/p/w500/uXDfjJbdP4ijW5hWSBrPrlKpxab.jpg', 'Brinquedos ganham vida quando os humanos não estão por perto.', 6.90, 24.90),
        ('Pantera Negra', 'Ação / Super-herói', 2018, 'Ryan Coogler', 'https://image.tmdb.org/t/p/w500/uxzzxijgPIY7slzFvMotPv8wjKA.jpg', 'T’Challa retorna a Wakanda para assumir o trono e proteger seu povo.', 8.90, 34.90),
        ('Oppenheimer', 'Drama / Biografia', 2023, 'Christopher Nolan', 'https://image.tmdb.org/t/p/w500/ptpr0kGAckfQkJeJIt8st5dglvd.jpg', 'A história do físico J. Robert Oppenheimer e sua participação no Projeto Manhattan.', 10.90, 49.90),
        ('Barbie', 'Comédia / Aventura', 2023, 'Greta Gerwig', 'https://image.tmdb.org/t/p/w500/yRRuLt7sMBEQkHsd1S3KaaofZn7.jpg', 'Barbie deixa seu mundo perfeito e embarca em uma jornada no mundo real.', 9.90, 39.90),
        ('Duna', 'Ficção Científica / Aventura', 2021, 'Denis Villeneuve', 'https://image.tmdb.org/t/p/w500/d5NXSklXo0qyIYkgV94XAgMIckC.jpg', 'Paul Atreides precisa enfrentar intrigas políticas e proteger o futuro de seu povo.', 8.90, 34.90),
        ('Duna: Parte Dois', 'Ficção Científica / Aventura', 2024, 'Denis Villeneuve', 'https://image.tmdb.org/t/p/w500/8b8R8l88Qje9dn9OE8PY05Nxl1X.jpg', 'Paul une forças com Chani e os Fremen para buscar vingança e evitar um futuro sombrio.', 11.90, 54.90),
        ('Homem-Aranha: Através do Aranhaverso', 'Animação / Super-herói', 2023, 'Joaquim Dos Santos', 'https://image.tmdb.org/t/p/w500/8Vt6mWEReuy4Of61Lnj5Xj704m8.jpg', 'Miles Morales encontra novos Homens-Aranha em uma aventura multiversal.', 9.90, 39.90),
        ('John Wick 4', 'Ação / Suspense', 2023, 'Chad Stahelski', 'https://image.tmdb.org/t/p/w500/gh2bmprLtUQ8oXCSluzfqaicyrm.jpg', 'John Wick enfrenta novos inimigos em busca de sua liberdade definitiva.', 9.90, 39.90),
        ('Avatar: O Caminho da Água', 'Ficção Científica / Aventura', 2022, 'James Cameron', 'https://image.tmdb.org/t/p/w500/mbYQLLluS651W89jO7MOZcLSCUw.jpg', 'Jake Sully e sua família exploram novas regiões de Pandora e enfrentam ameaças antigas.', 8.90, 34.90),
        ('Top Gun: Maverick', 'Ação / Drama', 2022, 'Joseph Kosinski', 'https://image.tmdb.org/t/p/w500/62HCnUTziyWcpDaBO2i1DX17ljH.jpg', 'Maverick retorna para treinar uma nova geração de pilotos em uma missão arriscada.', 8.90, 34.90),
        ('Parasita', 'Suspense / Drama', 2019, 'Bong Joon-ho', 'https://image.tmdb.org/t/p/w500/igw938inb6Fy0YVcwIyxQ7Lu5FO.jpg', 'Uma família pobre se infiltra na vida de uma família rica, desencadeando consequências inesperadas.', 7.90, 29.90),
        ('Whiplash', 'Drama / Música', 2014, 'Damien Chazelle', 'https://image.tmdb.org/t/p/w500/7fn624j5lj3xTme2SgiLCeuedmO.jpg', 'Um jovem baterista enfrenta métodos extremos para alcançar a excelência.', 6.90, 24.90),
        ('Batman: O Cavaleiro das Trevas', 'Ação / Crime', 2008, 'Christopher Nolan', 'https://image.tmdb.org/t/p/w500/iGZX91hIqM9Uu0KGhd4MUaJ0Rtm.jpg', 'Batman enfrenta o Coringa em uma batalha moral pelo destino de Gotham.', 7.90, 29.90),
        ('Gladiador', 'Ação / Drama', 2000, 'Ridley Scott', 'https://image.tmdb.org/t/p/w500/ty8TGRuvJLPUmAR1H1nRIsgwvim.jpg', 'Um general romano busca vingança após perder sua família e sua honra.', 6.90, 24.90),
        ('Titanic', 'Romance / Drama', 1997, 'James Cameron', 'https://image.tmdb.org/t/p/w500/9xjZS2rlVxm8SFx8kPC3aIGCOYQ.jpg', 'Um romance nasce durante a viagem do famoso navio Titanic.', 6.90, 24.90),
        ('Super Mario Bros. O Filme', 'Animação / Aventura', 2023, 'Aaron Horvath e Michael Jelenic', 'https://image.tmdb.org/t/p/w500/ktU3MIeZtuEVRlMftgp0HMX2WR7.jpg', 'Mario e Luigi entram em uma aventura pelo Reino dos Cogumelos.', 8.90, 34.90),
    ]
    for title, genre, year, director, poster, desc, rent, buy in movies:
        movie = Movie.query.filter_by(title=title).first()
        if movie:
            movie.genre = genre
            movie.year = year
            movie.director = director
            movie.poster_url = poster
            movie.description = desc
            movie.rent_price = rent
            movie.buy_price = buy
        else:
            db.session.add(Movie(title=title, genre=genre, year=year, director=director, poster_url=poster, description=desc, rent_price=rent, buy_price=buy))
    db.session.commit()


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if not name or not email or not password:
            flash('Preencha todos os campos.', 'danger')
            return redirect(url_for('register'))
        if password != confirm_password:
            flash('As senhas não conferem.', 'danger')
            return redirect(url_for('register'))
        if User.query.filter_by(email=email).first():
            flash('E-mail já cadastrado.', 'danger')
            return redirect(url_for('register'))
        user = User(name=name, email=email, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        for list_name in ['Assistir depois', 'Favoritos']:
            db.session.add(MovieList(name=list_name, user_id=user.id))
        db.session.commit()
        flash('Cadastro realizado com sucesso. Faça login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash('E-mail ou senha inválidos.', 'danger')
            return redirect(url_for('login'))
        session['user_id'] = user.id
        flash('Login realizado com sucesso.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Você saiu da conta.', 'info')
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    user = current_user()
    total_lists = MovieList.query.filter_by(user_id=user.id).count()
    total_watched = WatchedMovie.query.filter_by(user_id=user.id).count()
    total_movies = Movie.query.count()
    total_orders = Transaction.query.filter_by(user_id=user.id).count()
    recent_movies = Movie.query.order_by(Movie.id.desc()).limit(4).all()
    return render_template('dashboard.html', total_lists=total_lists, total_watched=total_watched, total_movies=total_movies, total_orders=total_orders, recent_movies=recent_movies)

@app.route('/movies')
@login_required
def movies():
    query = request.args.get('q', '').strip()
    genre = request.args.get('genre', '').strip()
    movies_query = Movie.query
    if query:
        movies_query = movies_query.filter(Movie.title.ilike(f'%{query}%'))
    if genre:
        movies_query = movies_query.filter(Movie.genre.ilike(f'%{genre}%'))
    all_movies = movies_query.order_by(Movie.title).all()
    genres = sorted({movie.genre for movie in Movie.query.all()})
    return render_template('movies.html', movies=all_movies, query=query, genre=genre, genres=genres)

@app.route('/movies/<int:movie_id>')
@login_required
def movie_detail(movie_id):
    movie = Movie.query.get_or_404(movie_id)
    user_lists = MovieList.query.filter_by(user_id=session['user_id']).all()
    watched = WatchedMovie.query.filter_by(user_id=session['user_id'], movie_id=movie_id).first() is not None
    purchased = Transaction.query.filter_by(user_id=session['user_id'], movie_id=movie_id, type='compra', status='aprovado').first() is not None
    active_rental = Transaction.query.filter(Transaction.user_id == session['user_id'], Transaction.movie_id == movie_id, Transaction.type == 'aluguel', Transaction.status == 'aprovado', Transaction.expires_at > datetime.utcnow()).first()
    return render_template('movie_detail.html', movie=movie, user_lists=user_lists, watched=watched, purchased=purchased, active_rental=active_rental)

@app.route('/movies/<int:movie_id>/checkout/<action_type>', methods=['GET', 'POST'])
@login_required
def checkout(movie_id, action_type):
    movie = Movie.query.get_or_404(movie_id)
    if action_type not in ['aluguel', 'compra']:
        flash('Tipo de operação inválido.', 'danger')
        return redirect(url_for('movie_detail', movie_id=movie_id))
    price = movie.rent_price if action_type == 'aluguel' else movie.buy_price
    pix_amount = DEMO_PIX_VALUE if USE_DEMO_PIX_VALUE else price
    if request.method == 'POST':
        expires_at = datetime.utcnow() + timedelta(days=2) if action_type == 'aluguel' else None
        transaction = Transaction(
            user_id=session['user_id'],
            movie_id=movie.id,
            type=action_type,
            price=price,
            payment_method='pix',
            status='pendente',
            expires_at=expires_at
        )
        db.session.add(transaction)
        db.session.commit()
        try:
            create_pix_payment(transaction, current_user())
        except Exception as error:
            db.session.delete(transaction)
            db.session.commit()
            flash(str(error), 'danger')
            return redirect(url_for('checkout', movie_id=movie_id, action_type=action_type))
        flash('Pix gerado. O filme será liberado automaticamente após a aprovação do pagamento.', 'info')
        return redirect(url_for('payment_pending', transaction_id=transaction.id))
    return render_template('checkout.html', movie=movie, action_type=action_type, price=price, pix_amount=pix_amount, use_demo=USE_DEMO_PIX_VALUE)

@app.route('/pagamento/<int:transaction_id>')
@login_required
def payment_pending(transaction_id):
    transaction = Transaction.query.get_or_404(transaction_id)
    if transaction.user_id != session['user_id']:
        flash('Acesso negado.', 'danger')
        return redirect(url_for('my_movies'))
    return render_template('payment_pending.html', transaction=transaction)

@app.route('/pagamento/<int:transaction_id>/verificar', methods=['POST'])
@login_required
def check_payment(transaction_id):
    transaction = Transaction.query.get_or_404(transaction_id)
    if transaction.user_id != session['user_id']:
        flash('Acesso negado.', 'danger')
        return redirect(url_for('my_movies'))
    if transaction.mp_payment_id:
        sync_payment_status(transaction.mp_payment_id)
    if transaction.is_released:
        flash('Pagamento aprovado! Filme liberado.', 'success')
        return redirect(url_for('my_movies'))
    flash('Pagamento ainda não aprovado. Tente novamente após pagar o Pix.', 'warning')
    return redirect(url_for('payment_pending', transaction_id=transaction.id))

@app.route('/webhook/mercado-pago', methods=['POST', 'GET'])
def mercado_pago_webhook():
    data = request.get_json(silent=True) or {}
    payment_id = None
    if data.get('type') == 'payment':
        payment_id = (data.get('data') or {}).get('id')
    payment_id = payment_id or request.args.get('data.id') or request.args.get('id')
    if payment_id:
        sync_payment_status(str(payment_id))
    return 'OK', 200

@app.route('/my-movies')
@login_required
def my_movies():
    items = Transaction.query.filter_by(user_id=session['user_id']).order_by(Transaction.created_at.desc()).all()
    now = datetime.utcnow()
    return render_template('my_movies.html', items=items, now=now)

@app.route('/lists')
@login_required
def lists():
    user_lists = MovieList.query.filter_by(user_id=session['user_id']).order_by(MovieList.name).all()
    return render_template('lists.html', user_lists=user_lists)

@app.route('/lists/create', methods=['POST'])
@login_required
def create_list():
    name = request.form.get('name')
    if not name:
        flash('Digite um nome para a lista.', 'danger')
        return redirect(url_for('lists'))
    db.session.add(MovieList(name=name, user_id=session['user_id']))
    db.session.commit()
    flash('Lista criada com sucesso.', 'success')
    return redirect(url_for('lists'))

@app.route('/lists/<int:list_id>')
@login_required
def list_detail(list_id):
    movie_list = MovieList.query.get_or_404(list_id)
    if movie_list.user_id != session['user_id']:
        flash('Você não tem permissão para acessar esta lista.', 'danger')
        return redirect(url_for('lists'))
    return render_template('list_detail.html', movie_list=movie_list)

@app.route('/lists/<int:list_id>/delete', methods=['POST'])
@login_required
def delete_list(list_id):
    movie_list = MovieList.query.get_or_404(list_id)
    if movie_list.user_id != session['user_id']:
        flash('Acesso negado.', 'danger')
        return redirect(url_for('lists'))
    db.session.delete(movie_list)
    db.session.commit()
    flash('Lista removida.', 'info')
    return redirect(url_for('lists'))

@app.route('/movies/<int:movie_id>/add-to-list', methods=['POST'])
@login_required
def add_to_list(movie_id):
    list_id = request.form.get('list_id')
    movie = Movie.query.get_or_404(movie_id)
    movie_list = MovieList.query.get_or_404(list_id)
    if movie_list.user_id != session['user_id']:
        flash('Acesso negado.', 'danger')
        return redirect(url_for('movie_detail', movie_id=movie_id))
    if movie not in movie_list.movies:
        movie_list.movies.append(movie)
        db.session.commit()
        flash('Filme adicionado à lista.', 'success')
    else:
        flash('Este filme já está na lista.', 'warning')
    return redirect(url_for('movie_detail', movie_id=movie_id))

@app.route('/lists/<int:list_id>/remove/<int:movie_id>', methods=['POST'])
@login_required
def remove_from_list(list_id, movie_id):
    movie_list = MovieList.query.get_or_404(list_id)
    movie = Movie.query.get_or_404(movie_id)
    if movie_list.user_id != session['user_id']:
        flash('Acesso negado.', 'danger')
        return redirect(url_for('lists'))
    if movie in movie_list.movies:
        movie_list.movies.remove(movie)
        db.session.commit()
        flash('Filme removido da lista.', 'info')
    return redirect(url_for('list_detail', list_id=list_id))

@app.route('/movies/<int:movie_id>/toggle-watched', methods=['POST'])
@login_required
def toggle_watched(movie_id):
    watched = WatchedMovie.query.filter_by(user_id=session['user_id'], movie_id=movie_id).first()
    if watched:
        db.session.delete(watched)
        flash('Filme desmarcado como assistido.', 'info')
    else:
        db.session.add(WatchedMovie(user_id=session['user_id'], movie_id=movie_id))
        flash('Filme marcado como assistido.', 'success')
    db.session.commit()
    return redirect(request.referrer or url_for('movies'))

@app.route('/watched')
@login_required
def watched():
    watched_items = WatchedMovie.query.filter_by(user_id=session['user_id']).all()
    return render_template('watched.html', watched_items=watched_items)

with app.app_context():
    db.create_all()
    ensure_schema_columns()
    seed_movies()

if __name__ == '__main__':
    app.run(debug=True)
