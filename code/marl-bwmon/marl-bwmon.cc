#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>
#include <optional>

#include <arpa/inet.h>
#include <fcntl.h>
#define __STDC_FORMAT_MACROS
#include <inttypes.h>
#include <netinet/in.h>
#include <pcap/pcap.h>
#include <poll.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <unistd.h>

namespace ch = std::chrono;

// #define DEBUG

ch::high_resolution_clock::time_point stdifyTimeval(const struct timeval tv)
{
	return ch::high_resolution_clock::time_point(
		ch::seconds(tv.tv_sec) + ch::microseconds(tv.tv_usec));
}

const int LISTEN_PORT = 9932;
const bool TIME_PRINT_ERR = false;
const bool USE_UNIX_SOCK = true;
const char *SOCKET_PATH = "/tmp/bwmon-sock";
const int FLOW_PRUNE_AGE = 10; // Changed from 2, in 2025-09-17

// Store and update some stats about float64s.
struct Stat
{
	double total = 0.0;
	double std = 0.0;
	double mean = 0.0;
	uint64_t k = 0;

	bool report_update = false;

	Stat() {}

	void clear()
	{
		total = 0.0;
		std = 0.0;
		mean = 0.0;
		k = 0;
	}

	// formulae courtesy of https://www.johndcook.com/blog/standard_deviation/
	void update(double value)
	{
		auto i = k++;
		total += value;

		if (i == 0)
		{
			mean = value;
			return;
		}
		auto delta = value - mean;
		mean = mean + delta / i;

		auto delta2 = value - mean;
		std = std + delta * delta2;

		if (report_update)
		{
			std::cout << "sample: " << value
					  << " gave deltas " << delta
					  << " and " << delta2
					  << std::endl;
		}
	}

	double variance() const
	{
		return k >= 2
				   ? std / (k - 1)
				   : 0.0;
	}
};

struct FlowMeasurement
{
	int64_t flow_length;
	uint64_t size_in;
	uint64_t size_out;
	uint64_t delta_in;
	uint64_t delta_out;
	uint64_t packets_in_count;
	uint64_t packets_out_count;
	float packets_in_mean;
	float packets_in_variance;
	float packets_out_mean;
	float packets_out_variance;
	float iat_mean;
	float iat_variance;
	uint32_t ip;
};

struct InnerFlowStats
{
	// flow size
	uint64_t flow_size_in = 0;
	uint64_t flow_size_out = 0;

	// needed for delta rate
	uint64_t flow_size_in_prev = 0;
	uint64_t flow_size_out_prev = 0;

	// packet stats in this window
	Stat in_packets = Stat();
	Stat out_packets = Stat();
	Stat in_packets_window = Stat();
	Stat out_packets_window = Stat();

	// interarrival times
	Stat interarrivals = Stat();
	Stat interarrivals_window = Stat();

	// flow length
	ch::high_resolution_clock::time_point flow_start;
	ch::high_resolution_clock::time_point last_entry;

	// unseen?
	bool unseen = true;

	InnerFlowStats() {
		// interarrivals.report_update = true;
	};

	void update(ch::high_resolution_clock::time_point arr_time, uint64_t size, bool inbound)
	{
		auto dbl_size = (double)size;
		ch::duration<double, std::milli> iat = arr_time - last_entry;

		if (inbound)
		{
			flow_size_in += size;
			in_packets.update(dbl_size);
			in_packets_window.update(dbl_size);

			// for now, only track IATs on inbound packets.
			interarrivals.update(iat.count());
			interarrivals_window.update(iat.count());
		}
		else
		{
			flow_size_out += size;
			out_packets.update(dbl_size);
			out_packets_window.update(dbl_size);
		}

		last_entry = arr_time;
	}

	bool clearAndPrintStats(char *ip_str, ch::high_resolution_clock::time_point startTime, ch::high_resolution_clock::time_point endTime)
	{
		auto prune_age = ch::seconds(FLOW_PRUNE_AGE);

		// prune here if last packet rx'd was of a certain age,
		// AND there was no info gleaned in this window.
		// Don't print stats in that case.

		auto duration = endTime - flow_start;
		auto silent_duration = endTime - last_entry;

		if (silent_duration > prune_age && last_entry < startTime)
		{
			return false;
		}

		std::cout
			<< ch::nanoseconds(duration).count() << ","
			<< flow_size_in << ","
			<< flow_size_out << ","
			<< flow_size_in - flow_size_in_prev << ","
			<< flow_size_out - flow_size_out_prev << ","
			<< in_packets_window.mean << ","
			<< in_packets_window.variance() << ","
			<< in_packets_window.k << ","
			<< out_packets_window.mean << ","
			<< out_packets_window.variance() << ","
			<< out_packets_window.k << ","
			<< interarrivals_window.mean << ","
			<< interarrivals_window.variance();
		clear();

		flow_size_in_prev = flow_size_in;
		flow_size_out_prev = flow_size_out;

		unseen = false;
		return true;
	}

	// FIXED: 2025-09-17
	std::optional<std::pair<bool, FlowMeasurement>> clearAndRetrieveStats(uint32_t ip, char * /*ip_str*/,
																		  ch::high_resolution_clock::time_point startTime,
																		  ch::high_resolution_clock::time_point endTime)
	{
		// Pruning policy (optional): if you want pruning, re-enable the checks below.
		// auto prune_age = ch::seconds(FLOW_PRUNE_AGE);
		// auto silent_duration = endTime - last_entry;
		// if (silent_duration > prune_age && last_entry < startTime) {
		//     return std::nullopt;
		// }

		auto duration = endTime - startTime;

		FlowMeasurement fm{
			ch::nanoseconds(endTime - flow_start).count(),		 // flow_length
			flow_size_in,										 // size_in
			flow_size_out,										 // size_out
			flow_size_in - flow_size_in_prev,					 // delta_in
			flow_size_out - flow_size_out_prev,					 // delta_out
			in_packets_window.k,								 // packets_in_count
			out_packets_window.k,								 // packets_out_count
			static_cast<float>(in_packets_window.mean),			 // packets_in_mean
			static_cast<float>(in_packets_window.variance()),	 // packets_in_variance
			static_cast<float>(out_packets_window.mean),		 // packets_out_mean
			static_cast<float>(out_packets_window.variance()),	 // packets_out_variance
			static_cast<float>(interarrivals_window.mean),		 // iat_mean
			static_cast<float>(interarrivals_window.variance()), // iat_variance
			ip													 // ip (network-order u32)
		};

		// Prepare for next window
		flow_size_in_prev = flow_size_in;
		flow_size_out_prev = flow_size_out;
		in_packets_window.clear();
		out_packets_window.clear();
		interarrivals_window.clear();

		bool new_data = unseen;
		unseen = false;
		(void)duration; // if you later want to use it

		return std::make_optional(std::make_pair(new_data, fm));
	}

	void clear()
	{
		in_packets_window.clear();
		out_packets_window.clear();
		interarrivals_window.clear();
	}

	void clear_full()
	{
		clear();
	}
};

struct FlowStats
{
	std::unordered_map<uint32_t, InnerFlowStats> per_dest_stats_;

	FlowStats() {
		// interarrivals.report_update = true;
	};

	void update(ch::high_resolution_clock::time_point arr_time, uint64_t size, bool inbound, uint32_t internal_ip)
	{ // get the entry using internal_ip, then update IT.
		auto it = per_dest_stats_.find(internal_ip);

		if (it != per_dest_stats_.end())
		{
			it->second.update(arr_time, size, inbound);
		}
		else
		{
			InnerFlowStats new_stat;
			new_stat.update(arr_time, size, inbound);
			per_dest_stats_[internal_ip] = new_stat;
		}
	}

	bool clearAndPrintStats(char *ip_str, ch::high_resolution_clock::time_point startTime, ch::high_resolution_clock::time_point endTime)
	{
		if (per_dest_stats_.empty())
		{
			return false;
		}

		// Note: we'll be told whether we need to prune each internal.
		// If we ever prune everything, then allow self to be pruned.

		std::cout << "(" << ip_str;

		std::vector<uint32_t> to_prune;

		for (auto &it : per_dest_stats_)
		{
			auto internal = it.first;

			char internal_ip_str[INET_ADDRSTRLEN];
			auto c_ip = reinterpret_cast<const in_addr *>(&internal);
			inet_ntop(AF_INET, c_ip, internal_ip_str, INET_ADDRSTRLEN);

			std::cout << "|" << internal_ip_str << ",";

			auto active =
				it.second.clearAndPrintStats(ip_str, startTime, endTime);

			if (!active)
			{
				to_prune.push_back(internal);
			}
		}

		std::cout << ")";

		for (auto &el : to_prune)
		{
			per_dest_stats_.erase(el);
		}

		return true;
	}

	// FIXED - 2025-09-17
	std::optional<std::pair<bool, FlowMeasurement>> clearAndRetrieveStats(uint32_t ip, char * /*ip_str*/,
																		  ch::high_resolution_clock::time_point startTime,
																		  ch::high_resolution_clock::time_point endTime)
	{
		// Aggregate all per-destination (InnerFlowStats) windows for this external IP.
		const auto dur_ns =
			std::chrono::duration_cast<std::chrono::nanoseconds>(endTime - startTime).count();

		// Byte totals & deltas
		uint64_t size_in = 0, size_out = 0;
		uint64_t delta_in = 0, delta_out = 0;

		// Packet counts (sum)
		uint64_t pkts_in = 0, pkts_out = 0;

		// Pooled means/variances via Welford (sample variance)
		auto combine = [](uint64_t k, double m, double var,
						  uint64_t &K, double &Mean, double &M2)
		{
			if (k == 0)
				return;
			const double M2_i = (k >= 2) ? var * double(k - 1) : 0.0; // sample var → M2
			if (K == 0)
			{
				K = k;
				Mean = m;
				M2 = M2_i;
				return;
			}
			const double delta = m - Mean;
			const uint64_t newK = K + k;
			Mean += delta * (double)k / (double)newK;
			M2 += M2_i + delta * delta * ((double)K * (double)k / (double)newK);
			K = newK;
		};

		uint64_t ps_in_K = 0, ps_out_K = 0, iat_K = 0;
		double ps_in_Mean = 0.0, ps_out_Mean = 0.0, iat_Mean = 0.0;
		double ps_in_M2 = 0.0, ps_out_M2 = 0.0, iat_M2 = 0.0;

		bool any_new = false;
		std::vector<uint32_t> to_prune;

		for (auto &kv : per_dest_stats_)
		{
			auto &inner = kv.second;
			auto m = inner.clearAndRetrieveStats(ip, nullptr, startTime, endTime);
			if (!m)
			{
				to_prune.push_back(kv.first);
				continue;
			}

			any_new |= m->first;
			const FlowMeasurement &fm = m->second;

			// bytes / deltas / counts
			size_in += fm.size_in;
			size_out += fm.size_out;
			delta_in += fm.delta_in;
			delta_out += fm.delta_out;

			pkts_in += fm.packets_in_count;
			pkts_out += fm.packets_out_count;

			// packet-size stats (in/out)
			combine(fm.packets_in_count, fm.packets_in_mean, fm.packets_in_variance,
					ps_in_K, ps_in_Mean, ps_in_M2);
			combine(fm.packets_out_count, fm.packets_out_mean, fm.packets_out_variance,
					ps_out_K, ps_out_Mean, ps_out_M2);

			// IAT stats: you track IATs only for inbound packets; use inbound count as k
			combine(fm.packets_in_count, fm.iat_mean, fm.iat_variance,
					iat_K, iat_Mean, iat_M2);
		}

		// prune inactive inners after iterating
		for (auto ip_inner : to_prune)
			per_dest_stats_.erase(ip_inner);

		// nothing left? return empty
		if (size_in == 0 && size_out == 0 && delta_in == 0 && delta_out == 0 &&
			pkts_in == 0 && pkts_out == 0 && ps_in_K == 0 && ps_out_K == 0 && iat_K == 0)
		{
			return std::nullopt;
		}

		// Build one aggregate measurement for this external IP
		FlowMeasurement agg{};
		agg.flow_length = (int64_t)dur_ns;
		agg.size_in = size_in;
		agg.size_out = size_out;
		agg.delta_in = delta_in;
		agg.delta_out = delta_out;
		agg.packets_in_count = pkts_in;
		agg.packets_out_count = pkts_out;
		agg.packets_in_mean = (float)ps_in_Mean;
		agg.packets_in_variance = (float)((ps_in_K >= 2) ? (ps_in_M2 / (double)(ps_in_K - 1)) : 0.0);
		agg.packets_out_mean = (float)ps_out_Mean;
		agg.packets_out_variance = (float)((ps_out_K >= 2) ? (ps_out_M2 / (double)(ps_out_K - 1)) : 0.0);
		agg.iat_mean = (float)iat_Mean;
		agg.iat_variance = (float)((iat_K >= 2) ? (iat_M2 / (double)(iat_K - 1)) : 0.0);
		agg.ip = ip;

		return std::make_optional(std::make_pair(any_new, agg));
	}
};

struct ValueSet
{
	ch::high_resolution_clock::time_point time;
	int64_t duration_ns;
	std::vector<uint64_t> good_bytes;
	std::vector<uint64_t> bad_bytes;
	std::vector<std::vector<FlowMeasurement>> flows;
};

class InterfaceStats
{
	int num_interfaces_;
	std::vector<uint64_t> bad_byte_counts_;
	std::vector<uint64_t> good_byte_counts_;

	std::vector<bool> importants_;
	std::vector<std::unordered_map<uint32_t, FlowStats>> flow_stats_;

	bool limiting_ = false;
	ch::high_resolution_clock::time_point limit_;
	std::atomic<int> to_go_ = std::atomic<int>(0);

	bool end_ = false;

	mutable std::shared_mutex mutex_;
	std::condition_variable_any stopped_limiting_;
	std::condition_variable_any hit_limit_;

public:
	InterfaceStats(int n)
		: num_interfaces_(n), bad_byte_counts_(std::vector<uint64_t>(2 * n, 0)), good_byte_counts_(std::vector<uint64_t>(2 * n, 0)), importants_(std::vector<bool>(n, false)), flow_stats_(std::vector<std::unordered_map<uint32_t, FlowStats>>(n, std::unordered_map<uint32_t, FlowStats>()))
	{
	}

	void markImportant(int n)
	{
		importants_[n] = true;
	}

	bool isImportant(int n)
	{
		return importants_.at(n);
	}

	void incrementStat(uint32_t internal_ip, uint32_t external_ip, int interface, bool good, uint32_t packetSize, bool inbound, ch::high_resolution_clock::time_point arr_time)
	{
		std::shared_lock<std::shared_mutex> lock(mutex_);

		auto index = 2 * interface + (inbound ? 0 : 1);
		auto important = isImportant(interface);

		if (good)
		{
			good_byte_counts_[index] += packetSize;
		}
		else
		{
			bad_byte_counts_[index] += packetSize;
		}

		if (important)
		{
			// first things first: check if the target IP has registered flowstats.
			auto &stat_holder = flow_stats_.at(interface);
			auto it = stat_holder.find(external_ip);

			if (it != stat_holder.end())
			{
				it->second.update(arr_time, packetSize, inbound, internal_ip);
			}
			else
			{
				FlowStats new_stat;
				new_stat.update(arr_time, packetSize, inbound, internal_ip);
				stat_holder[external_ip] = new_stat;
			}
		}
	}

	// FIXED: 2025-09-15: for potential read deadlock
	ValueSet clearAndRetrieveStats(ch::high_resolution_clock::time_point startTime,
								   std::vector<uint32_t> &allowed_ips)
	{
		// 1) Arm the limiter under the exclusive lock.
		{
			std::unique_lock<std::shared_mutex> lock(mutex_);
			limiting_ = true;
			limit_ = ch::high_resolution_clock::now();
			to_go_.store(num_interfaces_);
		}

		// 2) Wait for all capture threads to observe 'limit_' WITHOUT holding the lock.
		while (to_go_.load(std::memory_order_acquire) > 0)
		{
			std::this_thread::sleep_for(std::chrono::microseconds(50));
		}

		// 3) Reacquire the lock and build the snapshot.
		std::unique_lock<std::shared_mutex> lock(mutex_);
		const auto endTime = limit_;
		const int64_t duration_ns = std::chrono::nanoseconds(endTime - startTime).count();

		auto goods = good_byte_counts_;
		auto bads = bad_byte_counts_;

		std::vector<std::vector<FlowMeasurement>> flows;
		flows.reserve(flow_stats_.size());

#ifdef DEBUG
		for (auto aip : allowed_ips)
		{
			std::cout << "Allowed IP : " << aip << std::endl;
		}
#endif

		for (size_t i = 0; i < flow_stats_.size(); ++i)
		{
			if (!isImportant(static_cast<int>(i)))
			{
#ifdef DEBUG
				std::cout << "The flow " << i << " is not important" << std::endl;
#endif
				continue;
			}

			auto &outer_map = flow_stats_[i]; // external_ip -> FlowStats
			std::vector<FlowMeasurement> local_flows;
			std::vector<uint32_t> prune_externals;

			for (auto &ext_pair : outer_map)
			{
				auto &fs = ext_pair.second; // FlowStats
#ifdef DEBUG
				std::cout << "This flow key is " << ext_pair.first << std::endl;
#endif
				std::vector<uint32_t> prune_internals;

				for (auto &in_pair : fs.per_dest_stats_)
				{
					const uint32_t internal_ip = in_pair.first;
					auto &ifs = in_pair.second; // InnerFlowStats

					char ip_str[INET_ADDRSTRLEN];
					inet_ntop(AF_INET, reinterpret_cast<const in_addr *>(&internal_ip),
							  ip_str, INET_ADDRSTRLEN);

					auto maybe = ifs.clearAndRetrieveStats(internal_ip, ip_str, startTime, endTime);
					if (!maybe)
					{
						prune_internals.push_back(internal_ip);
						continue;
					}

					auto [is_new, fm] = *maybe;

					// const bool wanted =
					// 	is_new ||
					// 	(std::find(allowed_ips.begin(), allowed_ips.end(), internal_ip) != allowed_ips.end());
					// const uint32_t external_ip = ext_pair.first;  // outer_map key
					// const uint32_t external_ip_net = external_ip; // already network order if that is how stored
					// const uint32_t external_ip_host = ntohl(external_ip);
					fm.ip = htonl(internal_ip);

					// const bool match_external =
					// 	(std::find(allowed_ips.begin(), allowed_ips.end(), external_ip_net) != allowed_ips.end()) ||
					// 	(std::find(allowed_ips.begin(), allowed_ips.end(), external_ip_host) != allowed_ips.end());
					// const bool match_external =
					// 	(std::find(allowed_ips.begin(), allowed_ips.end(), external_ip_net) != allowed_ips.end()) ||
					// 	(std::find(allowed_ips.begin(), allowed_ips.end(), external_ip_host) != allowed_ips.end());
					// const bool wanted =
					// 	is_new ||
					// 	(std::find(allowed_ips.begin(), allowed_ips.end(), internal_ip) != allowed_ips.end());
					const uint32_t internal_ip_host = internal_ip;
					const uint32_t internal_ip_net = htonl(internal_ip);

					const bool match_internal =
						(std::find(allowed_ips.begin(), allowed_ips.end(), internal_ip_net) != allowed_ips.end()) ||
						(std::find(allowed_ips.begin(), allowed_ips.end(), internal_ip_host) != allowed_ips.end());

					const bool wanted = is_new || match_internal;
					// const bool wanted = is_new || match_external;

#ifdef DEBUG
					std::cout << "Finding internal ip " << internal_ip << std::endl;
#endif
					if (wanted)
						local_flows.emplace_back(fm);
				}
				for (auto ip : prune_internals)
					fs.per_dest_stats_.erase(ip);
				if (fs.per_dest_stats_.empty())
					prune_externals.push_back(ext_pair.first);
			}
			for (auto ip : prune_externals)
				outer_map.erase(ip);
			flows.emplace_back(std::move(local_flows));
		}

		// Reset counters for next window
		std::fill(good_byte_counts_.begin(), good_byte_counts_.end(), 0);
		std::fill(bad_byte_counts_.begin(), bad_byte_counts_.end(), 0);

		limiting_ = false;
		stopped_limiting_.notify_all();

		return ValueSet{endTime, duration_ns, goods, bads, flows};
	}

	ch::high_resolution_clock::time_point clearAndPrintStats(ch::high_resolution_clock::time_point startTime)
	{
		std::unique_lock<std::shared_mutex> lock(mutex_);

		auto endTime = ch::high_resolution_clock::now();
		auto duration = endTime - startTime;

		// First, tell the threads they have a limit TO WORK UP TO.
		limiting_ = true;
		limit_ = endTime;
		to_go_.store(num_interfaces_);

		// Okay, now await the signal from all the workers...
		int t_g = num_interfaces_;
		while ((t_g = to_go_.load()) > 0)
			hit_limit_.wait(lock);

		// Then, we need to wait for them to finish before we do all this...
		std::cout << ch::nanoseconds(duration).count() << "ns";

		for (unsigned int i = 0; i < bad_byte_counts_.size(); ++i)
		{
			std::cout << ", ";

			std::cout << good_byte_counts_[i] << " " << bad_byte_counts_[i];
		}

		std::cout << std::endl;

		// print (on a seperate line) the stats that concern the flows at each learner.
		for (unsigned int i = 0; i < flow_stats_.size(); ++i)
		{
			if (!isImportant(i))
			{
				continue;
			}

			std::cout << "[";
			auto &ip_map = flow_stats_.at(i);
			auto to_prune = std::vector<uint32_t>();

			for (auto &el : ip_map)
			{
				// el = (ip_as_u32, FlowStat)
				char ip_str[INET_ADDRSTRLEN];
				auto c_ip = reinterpret_cast<const in_addr *>(&el.first);
				inet_ntop(AF_INET, c_ip, ip_str, INET_ADDRSTRLEN);

				auto active = el.second.clearAndPrintStats(ip_str, startTime, endTime);
				if (!active)
				{
					to_prune.emplace_back(el.first);
				}
			}

			for (auto &ip : to_prune)
			{
				ip_map.erase(ip);
			}

			std::cout << "]";

			// std::cerr << "C: " << i << std::endl;
		}

		std::cout << std::endl;

		// Empty the stats.
		for (auto &el : good_byte_counts_)
			el = 0;
		for (auto &el : bad_byte_counts_)
			el = 0;

		limiting_ = false;

		// Signal done.
		stopped_limiting_.notify_all();

		return endTime;
	}

	bool finished()
	{
		std::shared_lock<std::shared_mutex> lock(mutex_);
		return end_;
	}

	void signalEnd()
	{
		std::unique_lock<std::shared_mutex> lock(mutex_);
		end_ = true;
	}

	void checkCanRecord(const struct timeval tv, const int id)
	{
		auto tv_convert = stdifyTimeval(tv);

		checkCanRecord(tv_convert, id);
	}

	void checkCanRecord(const ch::high_resolution_clock::time_point tv_convert, const int id)
	{
		std::shared_lock<std::shared_mutex> lock(mutex_);

		// Need to (if we hit the limit), signal that we *have*,
		// and then await the main thread signalling that it finished its read/write.
		if (limiting_ && tv_convert >= limit_)
		{
			// Signal.
			to_go_ -= 1;
			hit_limit_.notify_all();

			// Await using our lock and the reader's signal.
			// Don't loop here: the cond is guaranteed to be valid,
			// and control flow MUST escape.
			stopped_limiting_.wait(lock);
		}
	}
};

static inline uint32_t mask_from_prefix_host(int pfx)
/* Return a /pfx mask in **host** byte order (e.g., /24 -> 0xFFFFFF00 on LE) */
{
	if (pfx <= 0)
		return 0u;
	if (pfx >= 32)
		return 0xFFFFFFFFu;
	// build in host order; shift is defined since 0 < pfx < 32
	return 0xFFFFFFFFu << (32 - pfx);
}

struct PcapLoopParams
{
	PcapLoopParams(InterfaceStats &s, pcap_t *p, int i, int link,
				   const char *cidr_subnet = "10.0.0.0", int prefix = 24)
		: stats(s), iface(p), index(i), linkType(link)
	{
		// Store subnet and mask in HOST order.
		in_addr in{};
		if (inet_pton(AF_INET, cidr_subnet, &in) != 1)
		{
			// fallback to 0.0.0.0/0 if the string is bad
			subnet = 0u;
			netmask = 0u;
		}
		else
		{
			subnet = ntohl(in.s_addr);
			netmask = mask_from_prefix_host(prefix);
			// normalize subnet to network address
			subnet &= netmask;
		}
	}

	InterfaceStats &stats;
	pcap_t *iface;
	const int index;
	const int linkType;

	// Host-order network/subnet
	uint32_t subnet;  // e.g., 10.0.0.0 as 0x0A000000 **host order**
	uint32_t netmask; // e.g., /24 as 0xFFFFFF00 **host order**

	// Expects addr in **host** order
	bool is_ip_local(uint32_t addr) const
	{
		return (addr & netmask) == subnet;
	}
};

static bool parse_ipv4_addrs_EN10MB(const u_char *data, uint32_t caplen,
									uint32_t &src_host, uint32_t &dst_host,
									size_t &l3_off)
{
	// Ethernet header
	if (caplen < 14)
		return false;
	size_t off = 14;

	// Read EtherType (bytes 12..13)
	uint16_t ethertype;
	std::memcpy(&ethertype, data + 12, sizeof(ethertype));
	ethertype = ntohs(ethertype);

	// Skip VLAN/QinQ tags
	while (ethertype == 0x8100 /*802.1Q*/ || ethertype == 0x88a8 /*QinQ*/)
	{
		if (caplen < off + 4)
			return false; // not enough for VLAN tag
		// inner EtherType is at tag+2
		std::memcpy(&ethertype, data + off + 2, sizeof(ethertype));
		ethertype = ntohs(ethertype);
		off += 4;
	}

	if (ethertype != 0x0800)
		return false; // not IPv4

	// Need at least the minimal IPv4 header
	if (caplen < off + 20)
		return false;

	// Optionally verify IHL >= 5
	uint8_t ihl = data[off] & 0x0F;
	if (ihl < 5)
		return false;

	// Read src/dst (network order -> host order)
	uint32_t src_be, dst_be;
	std::memcpy(&src_be, data + off + 12, sizeof(src_be));
	std::memcpy(&dst_be, data + off + 16, sizeof(dst_be));
	src_host = ntohl(src_be);
	dst_host = ntohl(dst_be);
	l3_off = off;
	return true;
}

static void perPacketHandle(u_char *user, const struct pcap_pkthdr *h, const u_char *data)
{
	PcapLoopParams *params = reinterpret_cast<PcapLoopParams *>(user);
	auto arr_time = stdifyTimeval(h->ts);

	params->stats.checkCanRecord(arr_time, params->index);

	// Look at the packet, decide good/bad, then increment!
	// Establish the facts: HERE.
	// Okay, we can read up to h->caplen bytes from data.
	uint32_t src_ip_h = 0, dst_ip_h = 0;
	size_t l3off = 0;

	switch (params->linkType)
	{
	case DLT_NULL:
	{
		// BSD loopback: 4-byte AF_*
		if (h->caplen < 4 + 20)
			return;
		size_t off = 4;
		uint32_t s, d;
		std::memcpy(&s, data + off + 12, 4);
		std::memcpy(&d, data + off + 16, 4);
		src_ip_h = ntohl(s);
		dst_ip_h = ntohl(d);
		break;
	}
	case DLT_EN10MB:
	{
		if (!parse_ipv4_addrs_EN10MB(data, h->caplen, src_ip_h, dst_ip_h, l3off))
			return;
		break;

		break;
	}
	case DLT_RAW:
	{
		if (h->caplen < 20)
			return;
		uint32_t s, d;
		std::memcpy(&s, data + 12, 4);
		std::memcpy(&d, data + 16, 4);
		src_ip_h = ntohl(s);
		dst_ip_h = ntohl(d);
		break;
	}
	default:
		std::cerr << "Unknown linktype for iface "
				  << params->index << ": saw " << params->linkType << std::endl;
	}
	// Determine outbound/external using host-order addresses
	bool outbound = params->is_ip_local(src_ip_h);
	uint32_t internal_ip = outbound ? src_ip_h : dst_ip_h;
	uint32_t external_ip = outbound ? dst_ip_h : src_ip_h;

	// If you want the first octet reliably:
	auto first_octet = (external_ip >> 24) & 0xFF; // now correct after ntohl()
	bool good = !(first_octet % 2);

	// #ifdef DEBUG
	// 	std::cout << "Tracing a packet with  " << src_ip_h << "->" << dst_ip_h << std::endl
	// 			  << "This src is local " << outbound << std::endl
	// 			  << "The good flag is " << good << std::endl;
	// #endif /*DEBUG*/
	params->stats.incrementStat(internal_ip, external_ip, params->index, good, h->len, !outbound, arr_time);
}

static void monitorInterface(pcap_t *iface, const int index, InterfaceStats &stats)
{
	int err = 0;
	int fd = 0;
	char errbuff[PCAP_ERRBUF_SIZE];

	if ((err =
			 pcap_set_immediate_mode(iface, 1) || pcap_activate(iface)) ||
		pcap_setnonblock(iface, 1, errbuff))
	{
		std::cerr << "iface " << index << " could not be initialised: ";

		switch (err)
		{
		case PCAP_ERROR_NO_SUCH_DEVICE:
			std::cerr << "no such device.";
			break;
		case PCAP_ERROR_PERM_DENIED:
			std::cerr << "bad permissions; run as sudo?";
			break;
		default:
			std::cerr << "something unknown.";
		}

		std::cerr << std::endl;
	}

	if (!err)
	{
		int linkType = pcap_datalink(iface);
		auto params = PcapLoopParams(stats, iface, index, linkType);

		if ((fd = pcap_get_selectable_fd(iface)) < 0)
			std::cerr << "Weirdly, got fd " << fd << "." << std::endl;

		struct pollfd iface_pollfd = {
			fd,
			POLLIN,
			0};

		// pcap_loop(iface, -1, perPacketHandle, reinterpret_cast<u_char *>(&params));

		int count_processed = 0;
		while (count_processed >= 0)
		{
			auto n_evts = poll(&iface_pollfd, 1, 1);

			if (n_evts > 0)
				count_processed = pcap_dispatch(
					iface, -1, perPacketHandle, reinterpret_cast<u_char *>(&params));
			if (n_evts == 0 || !count_processed)
				stats.checkCanRecord(ch::high_resolution_clock::now(), index);

			if (stats.finished())
			{
				break;
			}
		}
	}
	else
	{
		std::cerr << "Error setting up pcap: " << errbuff << ", " << pcap_geterr(iface) << std::endl;
	}

	pcap_close(iface);
	std::this_thread::yield();
}

static bool read_val(int fd, void *location, size_t len, InterfaceStats &stats)
{
	size_t remaining = len;
	while (remaining && !stats.finished())
	{
		fd_set rfds;
		FD_ZERO(&rfds);
		FD_SET(fd, &rfds);

		struct timeval tv;
		tv.tv_sec = 5; // give the client time between requests
		tv.tv_usec = 0;

		int r = select(fd + 1, &rfds, nullptr, nullptr, &tv);
		if (r == 0)
		{
			// timeout -> just wait again, do NOT treat as error
			continue;
		}
		if (r < 0)
		{
			if (errno == EINTR)
				continue;
			return true; // error
		}

		ssize_t n = recv(fd,
						 (reinterpret_cast<char *>(location)) + (len - remaining),
						 remaining, 0);
		if (n == 0)
		{
			// real EOF (peer closed)
			return true;
		}
		if (n < 0)
		{
			if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
				continue;
			return true;
		}
		remaining -= static_cast<size_t>(n);
	}
	return false; // success
}

// static bool read_val(int fd, void *location, size_t len, InterfaceStats &stats)
// {
// 	auto remaining = len;
// 	auto err = false;

// 	timeval tv;
// 	fd_set selector;

// 	while (remaining && !err)
// 	{
// 		tv.tv_sec = 1;
// 		tv.tv_usec = 0;

// 		FD_ZERO(&selector);
// 		FD_SET(fd, &selector);
// 		select(fd + 1, &selector, nullptr, nullptr, &tv);

// 		ssize_t bytes_read = 0;

// 		if (FD_ISSET(fd, &selector))
// 		{
// 			bytes_read = recv(
// 				fd,
// 				(reinterpret_cast<char *>(location)) + (len - remaining),
// 				remaining,
// 				0);
// 		}

// 		// bytes_read == 0 means peer closed -> treat as error to unwind cleanly
// 		if (bytes_read <= 0 || stats.finished())
// 		{
// 			err = true;
// 			break;
// 		}
// 		else
// 		{
// 			remaining -= bytes_read;
// 		}
// 	}

// 	return err;
// }

static bool send_val(int fd, void *location, size_t len, InterfaceStats &stats)
{
	auto remaining = len;
	auto err = false;

	while (remaining && !err)
	{
		auto bytes_sent = 0;
		bytes_sent = send(
			fd,
			(reinterpret_cast<char *>(location)) + (len - remaining),
			remaining,
			0);

		if (bytes_sent < 0 || stats.finished())
		{
			err = true;
			break;
		}
		else
		{
			remaining -= bytes_sent;
		}
	}

	return err;
}

// FIXED: 2025-09-15: safer operations
static void server_runner(InterfaceStats &stats)
{
	using namespace std;
	auto startTime = ch::high_resolution_clock::now();

	// 1) Create a UNIX domain socket at SOCKET_PATH
	int server_fd = socket(PF_LOCAL, SOCK_STREAM, 0);
	if (server_fd < 0)
	{
		perror("socket(AF_UNIX)");
		exit(EXIT_FAILURE);
	}

	int opt = 1;
	if (setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR | SO_REUSEPORT | SO_KEEPALIVE,
				   &opt, sizeof(opt)) != 0)
	{
		perror("setsockopt");
		close(server_fd);
		exit(EXIT_FAILURE);
	}

	// Bind absolute path (unlink stale path first), then listen
	sockaddr_un uaddr{};
	memset(&uaddr, 0, sizeof(uaddr));
	uaddr.sun_family = AF_UNIX;
	// SOCKET_PATH already set to "/tmp/bwmon-sock"
	// (see the constants block at the top of the file)
	strncpy(uaddr.sun_path, SOCKET_PATH, sizeof(uaddr.sun_path) - 1);
	unlink(SOCKET_PATH);
	if (bind(server_fd, reinterpret_cast<sockaddr *>(&uaddr), sizeof(uaddr)) < 0)
	{
		perror("bind(AF_UNIX)");
		close(server_fd);
		exit(EXIT_FAILURE);
	}
	// Allow non-root Python to connect if needed
	chmod(SOCKET_PATH, 0666);

	if (listen(server_fd, 64) < 0) // FIXED: 2025-09-18: prevent the ECONNREFUSE
	{
		perror("listen");
		close(server_fd);
		exit(EXIT_FAILURE);
	}

	// 2) Accept/re-accept loop, one client at a time
	while (!stats.finished())
	{
		sockaddr_un raddr{};
		socklen_t rlen = sizeof(raddr);

		int conn_fd = accept(server_fd, reinterpret_cast<sockaddr *>(&raddr), &rlen);
		startTime = ch::high_resolution_clock::now();
		if (conn_fd < 0)
		{
			if (errno == EINTR)
				continue;
			perror("accept");
			break;
		}

		// 3) Per-connection request/response loop
		while (!stats.finished())
		{
			// Read a u32: number of flow IP queries (network byte order)
			uint32_t n_flow_queries = 0;
			if (read_val(conn_fd, &n_flow_queries, sizeof(n_flow_queries), stats))
			{
// client disconnected or error
#ifdef DEBUG
				std::cout << "Error reading the flow number" << std::endl;
#endif
				break;
			}
			n_flow_queries = ntohl(n_flow_queries);
#ifdef DEBUG
			std::cout << "Got a query for " << n_flow_queries << endl;
#endif
			// Read that many IPs (each u32 in network byte order).
			std::vector<uint32_t> flow_ips(n_flow_queries);
			if (n_flow_queries > 0)
			{
				const size_t fip_bytes = n_flow_queries * sizeof(uint32_t);
				if (read_val(conn_fd, flow_ips.data(), fip_bytes, stats))
				{
					perror("Error reading flow bytes\n");
					break;
				}
#ifdef DEBUG
				for (auto ip : flow_ips)
				{
					std::cout << "I got IP address " << ip << std::endl;
				}
#endif
				// NOTE: We keep them in network byte order intentionally.
				// The snapshot code compares raw u32 host addresses as stored
				// from packets; Python must send them packed as "!I".
			}
			const auto inTime = ch::high_resolution_clock::now();

			// Build a snapshot ending "now".
			// This also resets per-window counters for the next tick.
			ValueSet stat_block = stats.clearAndRetrieveStats(startTime, flow_ips); // returns time, duration_ns, good_bytes, bad_bytes, flows
			// Protocol framing expected by Python's ask_stats():
			//  - int64_t duration_ns
			//  - good_bytes[2*N] then bad_bytes[2*N]
			//  - for each important interface: u32 n_flows (network order), then n_flows * sizeof(FlowMeasurement)

			// Header: duration (ns)
			if (send_val(conn_fd, &stat_block.duration_ns, sizeof(int64_t), stats))
				goto connection_done;

			// Per-interface bytes (good then bad)
			for (uint64_t b : stat_block.good_bytes)
			{
				if (send_val(conn_fd, &b, sizeof(uint64_t), stats))
					goto connection_done;
			}
			for (uint64_t b : stat_block.bad_bytes)
			{
				if (send_val(conn_fd, &b, sizeof(uint64_t), stats))
					goto connection_done;
			}

			// Per-interface flow blocks
			for (const auto &flow_vec : stat_block.flows)
			{
				uint32_t n_flows = static_cast<uint32_t>(flow_vec.size());
				uint32_t n_flows_net = htonl(n_flows);
#ifdef DEBUG

#endif
				if (send_val(conn_fd, &n_flows_net, sizeof(uint32_t), stats))
					goto connection_done;

				if (n_flows > 0)
				{
					// FlowMeasurement layout must match Python's struct ("=q6Q6fI4x")
					// Ensure your compiler keeps the expected 88-byte size (the program prints it at startup).
					const size_t payload = n_flows * sizeof(FlowMeasurement);
					if (send_val(conn_fd, const_cast<FlowMeasurement *>(flow_vec.data()), payload, stats))
					{
						goto connection_done;
					}
				}
			}

			// Advance the next window start to the snapshot's time
			startTime = stat_block.time;

			const auto outTime = ch::high_resolution_clock::now();
			if (TIME_PRINT_ERR)
			{
				std::cerr << "C-time:" << ch::nanoseconds(outTime - inTime).count() / 1000000 << std::endl;
			}
		}
		// std::cout << "Connection terminated.\n";
	connection_done:
		shutdown(conn_fd, SHUT_RDWR);
		close(conn_fd);
		// Loop back to accept a fresh client (or exit if stats.finished()).
	}

	// 4) Clean up the listener
	close(server_fd);
	// (SOCKET_PATH will be unlinked on next bind; optional runtime unlink here)
	// unlink(SOCKET_PATH);
	std::this_thread::yield();
}

static void do_join(std::thread &t)
{
	t.join();
}

static void listDevices(char *errbuf)
{
	pcap_if_t *devs = nullptr;
	pcap_findalldevs(&devs, errbuf);

	while (devs != nullptr)
	{
		std::cout << devs->name;
		if (devs->description != nullptr)
			std::cout << ": " << devs->description;

		std::cout << std::endl;
		devs = devs->next;
	}

	pcap_freealldevs(devs);
}

int main(int argc, char const *argv[])
{
	char errbuf[PCAP_ERRBUF_SIZE];
	auto num_interfaces = argc - 1;
	auto start_pos = 1;
	auto server = false;

	std::vector<std::thread> workers;

	// catch server flag
	if (num_interfaces > 0 && !strcmp("-s", argv[start_pos]))
	{
		server = true;
		start_pos += 1;
		num_interfaces -= 1;
	}

	if (num_interfaces == 0)
	{
		listDevices(errbuf);
		printf("Struct size? %" PRIu64 "\n", sizeof(FlowMeasurement));
		return 0;
	}

	auto stats = InterfaceStats(num_interfaces);
	auto startTime = ch::high_resolution_clock::now();

	bool err = false;

	for (int i = 0; i < num_interfaces; ++i)
	{
		// iterate over iface names, spawn threads!
		// We want to catch any which start with a '!', these are]
		// important
		auto name = argv[start_pos + i];
		// auto individual_stats = name[0] == '!';
		auto individual_stats = true;
		if (individual_stats)
		{
			// name = &(name[1]);
			stats.markImportant(i);
		}

		auto p = pcap_create(name, errbuf);

		if (p == nullptr)
		{
			err = true;
			std::cerr << errbuf << std::endl;
			break;
		}

		// Can't copy or move these for whatever reason, so must emplace.
		// i.e. init RIGHT IN THE VECTOR
		workers.emplace_back(std::thread(monitorInterface, p, i, std::ref(stats)));
	}

	// If we're doing the server thing, we want a thread running that.
	// Its job is to handle connections (one at a time, allowing reconnection),
	// and respond to more granular stat requests as they come in.
	if (server)
	{
		workers.emplace_back(std::thread(server_runner, std::ref(stats)));
	}

	// Now block on next user input.
	std::string lineInput;

	if (!err)
	{
		while (1)
		{
			// Any (non-EOF) line will produce more output.
			std::getline(std::cin, lineInput);

			if (std::cin.eof())
				break;

			if (!server)
			{
				auto inTime = ch::high_resolution_clock::now();
				startTime = stats.clearAndPrintStats(startTime);

				auto outTime = ch::high_resolution_clock::now();
				if (TIME_PRINT_ERR)
				{
					std::cerr << "C-time:" << ch::nanoseconds(outTime - inTime).count() / 1000000 << std::endl;
				}
			}
		}
	}

	// Kill and cleanup if they send a signal or whatever.
	stats.signalEnd();
	std::for_each(workers.begin(), workers.end(), do_join);

	return 0;
}
