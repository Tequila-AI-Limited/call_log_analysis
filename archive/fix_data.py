import weekly_data_manager

start = '2026-04-13'
end = '2026-04-19'

data = weekly_data_manager.load_week_data(start, end)
print("Current DB data:", data)

if data:
    corrected_data = {
        'start_date': start,
        'end_date': end,
        'total': 2431,
        'retail': 1676,
        'trade': 475,
        'abandoned': data['abandoned_total'],
        'abandoned_retail': data['retail_abandoned'],
        'abandoned_trade': data['trade_abandoned']
    }
    
    weekly_data_manager.save_week_data(corrected_data)
    print("Corrected data saved to weekly_stats!")
else:
    print("No data found for this week.")
