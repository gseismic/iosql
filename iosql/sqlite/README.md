# partition

```
toYYYYMM('date')
[fn, 'date', min, max]

{
	'fn': fn, 
	'column': ['date', 'symbol'],
	'forward_fn': lambda x: x/100,
   	'reverse_min_fn': {
	   	'id': lambda part: part*100,
   	},
   	'reverse_max_fn': {
	   	'id': lambda part: (part+1)*100
   	}
}

{
	'key': ['date', 'symbol'],
	'map': lambda x: x/100,
   	'imap_min': {
	   	'id': lambda part: part*100,
   	},
   	'imap_max': {
	   	'id': lambda part: (part+1)*100
   	}
}
{
	'key': 'date',
	'map': 'toYYYYMM'
}
```
