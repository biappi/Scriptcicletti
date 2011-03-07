import pylast
from math import sqrt 

####
USERNAME = "FILL_YOUR_OWN"
APIKEY   = "FILL_YOUR_OWN"
####

def get_tags(track):
	tags     = track.get_top_tags()
	filtered = {}
	for t in tags:
		try:
			filtered[t.item.name]=int(t.weight)
		except:
			pass

	return filtered

def scalar(collection): 
  total = 0 
  for coin, count in collection.items(): 
    total += count * count 
  return sqrt(total) 

def similarity(A,B): 
  total = 0 
  for kind in A:
    if kind in B: 
      total += A[kind] * B[kind] 
  return float(total) / (scalar(A) * scalar(B))

if __name__ == '__main__':
	n = pylast.LastFMNetwork(username=USERNAME)
	n.api_key = APIKEY
	t1_tags = get_tags(n.get_track("the doors", "spanish caravan"))
	t2_tags = get_tags(n.get_track("jimi hendrix", "foxy lady"))
	print similarity(t1_tags, t2_tags)
