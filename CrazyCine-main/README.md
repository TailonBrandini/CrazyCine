# CrazyCine — Flask + Pix Mercado Pago

Projeto de Desenvolvimento Web em Flask com cadastro/login, catálogo de filmes, listas personalizadas, filmes assistidos, compra/aluguel e integração Pix com Mercado Pago.

## Funcionalidades

- Cadastro e login de usuários
- Catálogo com busca de filmes
- Listas personalizadas, como Favoritos e Assistir depois
- Marcação de filmes como assistidos
- Compra e aluguel de filmes
- Geração de Pix pelo Mercado Pago
- QR Code Pix e Pix copia e cola
- Webhook para liberar o filme automaticamente quando o pagamento for aprovado
- Página Meus Filmes com status do pagamento

## Como rodar

```bash
python -m venv venv
```

Windows:

```bash
venv\Scripts\activate
```

Instale as dependências:

```bash
pip install -r requirements.txt
```

Configure seu Access Token do Mercado Pago.

Windows PowerShell:

```powershell
$env:MERCADO_PAGO_ACCESS_TOKEN="SEU_ACCESS_TOKEN_AQUI"
```

Windows CMD:

```cmd
set MERCADO_PAGO_ACCESS_TOKEN=SEU_ACCESS_TOKEN_AQUI
```

Linux/Mac:

```bash
export MERCADO_PAGO_ACCESS_TOKEN="SEU_ACCESS_TOKEN_AQUI"
```

Rode:

```bash
python app.py
```

Acesse:

```text
http://127.0.0.1:5000
```

## Modo Pix de R$ 0,01

Por padrão, o projeto gera o Pix real no valor simbólico de R$ 0,01 para apresentação do trabalho.

Para cobrar o valor real do filme, configure:

```bash
USE_DEMO_PIX_VALUE=false
```

## Webhook do Mercado Pago

A rota do webhook é:

```text
/webhook/mercado-pago
```

Em produção, configure uma URL pública no Mercado Pago, por exemplo:

```text
https://seudominio.com/webhook/mercado-pago
```

Para testes locais, use ngrok:

```bash
ngrok http 5000
```

Depois configure:

Windows PowerShell:

```powershell
$env:WEBHOOK_BASE_URL="https://seu-link-ngrok.ngrok-free.app"
```

Se o webhook não estiver configurado, você ainda pode clicar no botão "Já paguei, verificar agora" na tela do Pix para consultar o status do pagamento.

## Observação importante

O Flask não verifica Pix diretamente pela chave Pix. A confirmação real vem pelo Mercado Pago, usando o Access Token e a consulta/webhook de pagamento.
