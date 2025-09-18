CXXFLAGS=-Wall -Werror -std=c++17 -g -O2 -march=native
LDFLAGS=-lpcap -lpthread -lsupc++

marl-bwmon: marl-bwmon.cc
	g++ $(CXXFLAGS) $< -o $@ $(LDFLAGS)
	@echo "Compiled marl-bwmon"

debug: marl-bwmon.cc
	g++ $(CXXFLAGS) -DDEBUG $< -o marl-bwmon $(LDFLAGS)
	@echo "Compiled marl-bwmon with DEBUG"

clean:
	rm -f marl-bwmon *.o
