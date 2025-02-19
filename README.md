## PROMPT
`I want a system using backend api (fastapi, sqlite, stripe) where i can create unique and safe payment links by setting amount, order_id and email. I want a system using backend api (fastapi, sqlite, stripe) where i can create unique and safe payment links by setting amount, order_id and email. when someone goes to the link on the browser they will see a button to make payment. For the page use html, css, boostrap, jquery. In that page they should be able to view the readonly information ( amount, order_id and email). Include important validation in the backend. and make the page expirable within 5 minutes. When someone goes to an expired page it should show it. when someone goes to a page where payment it already made it should show it. Store valualbe info in the db. Include graceful exception handling. i already have an .env file with STRIPE_PUBLIC_KEY and 
STRIPE_SECRET_KEY.


## RUN SERVER
uvicorn main:app --reload

