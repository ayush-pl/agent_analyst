Table sample_coffee_sales was uploaded from the file sample_coffee_sales.csv (5,699 rows, one row per record).
Date columns (sale_date) are ISO text 'YYYY-MM-DD': use strftime('%Y-%m', col) for months and strftime('%Y', col) for years.
Columns discount were percentages: 12.5 means 12.5%.
Original headers: 'Sale Date' is sale_date; 'Store' is store; 'Item' is item; 'Category' is category; 'Qty' is qty; 'Unit Price ($)' is unit_price; 'Discount %' is discount; 'Payment Method' is payment_method; 'Customer Type' is customer_type.
User definitions: revenue = qty * unit_price * (1 - discount / 100). discount is a percentage.